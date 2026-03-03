"""Standalone ASB worker process.

Run as:  python -m src.asb_consumer

Reads messages from the provisioning-queue, runs the provisioning agent,
and writes results to Azure Blob Storage.

Message format:
    {"run_id": "...", "ticket_id": "...", "max_iterations": 15}

Blob state transitions:
    queued → running → completed | failed

On exception the message is abandoned (ASB will redeliver up to
max_delivery_count=10 times, then dead-letter it).
"""
from __future__ import annotations

import json
import logging
import os
import signal
from datetime import datetime, timezone

from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient

from .blob_store import read_run, write_run
from .openai_client import load_openai_settings
from .provisioning_agent import provision_from_ticket
from .telemetry import track_provision_run

logger = logging.getLogger(__name__)

_shutdown = False


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _shutdown
    logger.info("Shutdown signal %s received — draining then stopping", signum)
    _shutdown = True


def _parse_body(msg) -> dict:  # noqa: ANN001
    """Extract JSON dict from a ServiceBusReceivedMessage body."""
    chunks = list(msg.body)
    if chunks and isinstance(chunks[0], (bytes, bytearray)):
        raw = b"".join(chunks)
    else:
        raw = str(chunks[0]).encode() if chunks else b"{}"
    return json.loads(raw)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Wire up Azure Monitor so logger.info() flows to App Insights
    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    if conn_str:
        try:
            from azure.monitor.opentelemetry import configure_azure_monitor
            configure_azure_monitor(connection_string=conn_str)
            logger.info("Azure Monitor telemetry enabled in worker")
        except Exception as exc:
            logger.warning("Azure Monitor setup failed (non-fatal): %s", exc)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    hostname = os.environ["AZURE_SERVICE_BUS_HOSTNAME"]
    queue_name = os.environ["AZURE_SERVICE_BUS_QUEUE_NAME"]
    logger.info("ASB consumer starting — queue=%s", queue_name)

    credential = DefaultAzureCredential()

    with ServiceBusClient(hostname, credential) as client:
        # max_wait_time: how long receive_messages blocks when queue is empty
        with client.get_queue_receiver(queue_name, max_wait_time=30) as receiver:
            while not _shutdown:
                messages = receiver.receive_messages(max_message_count=1, max_wait_time=30)

                for msg in messages:
                    if _shutdown:
                        receiver.abandon_message(msg)
                        break

                    run_id = "unknown"
                    started_at = datetime.now(timezone.utc)
                    body: dict = {}

                    try:
                        body = _parse_body(msg)
                        run_id = body["run_id"]
                        ticket_id = body["ticket_id"]
                        max_iterations = body.get("max_iterations", 15)

                        logger.info("Processing run_id=%s ticket=%s", run_id, ticket_id)

                        # ── Mark as running (best-effort) ─────────────────
                        run_data = {"run_id": run_id, "ticket_id": ticket_id}
                        try:
                            run_data = read_run(run_id) or run_data
                            run_data.update({
                                "status": "running",
                                "started_at": started_at.isoformat(),
                            })
                            write_run(run_id, run_data)
                        except Exception as _be:
                            logger.warning("Blob write (running) failed (non-fatal): %s", _be)

                        # ── Run agent ─────────────────────────────────────
                        settings = load_openai_settings()
                        result = provision_from_ticket(
                            openai_settings=settings,
                            ticket_id=ticket_id,
                            max_iterations=max_iterations,
                        )

                        # ── Mark as completed (best-effort) ───────────────
                        completed_at = datetime.now(timezone.utc)
                        run_data.update({
                            "status": "completed",
                            "completed_at": completed_at.isoformat(),
                            "pr_url": result.pr_url,
                            "summary": result.summary,
                            "ticket_updated": result.ticket_updated,
                            "iterations": result.iterations,
                        })
                        if result.eval_scores:
                            run_data["eval_scores"] = result.eval_scores
                        try:
                            write_run(run_id, run_data)
                        except Exception as _be:
                            logger.warning("Blob write (completed) failed (non-fatal): %s", _be)

                        track_provision_run(
                            ticket_id=ticket_id,
                            success=True,
                            iterations=result.iterations,
                            pr_url=result.pr_url,
                            ticket_updated=result.ticket_updated,
                            duration_seconds=(completed_at - started_at).total_seconds(),
                        )

                        receiver.complete_message(msg)
                        logger.info("Completed run_id=%s pr=%s", run_id, result.pr_url)

                    except Exception as exc:
                        logger.exception("Failed run_id=%s: %s", run_id, exc)

                        # Write failure state (best-effort)
                        try:
                            failed_at = datetime.now(timezone.utc)
                            run_data = read_run(run_id) or {"run_id": run_id}
                            run_data.update({
                                "status": "failed",
                                "failed_at": failed_at.isoformat(),
                                "error": str(exc)[:2000],
                            })
                            write_run(run_id, run_data)
                            track_provision_run(
                                ticket_id=body.get("ticket_id", ""),
                                success=False,
                                iterations=0,
                                error=str(exc),
                                duration_seconds=(failed_at - started_at).total_seconds(),
                            )
                        except Exception:
                            pass

                        # Abandon → ASB will redeliver (up to max_delivery_count)
                        receiver.abandon_message(msg)

    logger.info("ASB consumer stopped")


if __name__ == "__main__":
    main()
