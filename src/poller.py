"""Background poller — watches ServiceNow for newly approved RITM tickets
and automatically triggers the provisioning agent.

Behaviour:
- On first poll, records all currently approved tickets as "already seen"
  (so re-deploying doesn't re-provision old tickets).
- On every subsequent poll, any ticket that moves into approved state and
  hasn't been seen before is automatically provisioned.
- Works in both async mode (enqueues to ASB) and sync mode (runs inline).

Control via env vars:
  SNOW_POLL_INTERVAL_SECONDS  — how often to poll (default: 30)
  SNOW_POLL_ENABLED           — set to "false" to disable (default: true)
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_seen_tickets: set[str] = set()
_initialized: bool = False


def _get_approved_tickets() -> list[str]:
    """Direct REST call to SNOW — returns list of approved RITM ticket numbers."""
    instance = os.getenv("SERVICENOW_INSTANCE_URL", "").rstrip("/")
    user = os.getenv("SERVICENOW_USERNAME", "")
    password = os.getenv("SERVICENOW_PASSWORD", "")

    if not instance or not user or not password:
        return []

    url = f"{instance}/api/now/table/sc_req_item"
    params = {
        "sysparm_query": "approval=approved^active=true",
        "sysparm_fields": "number",
        "sysparm_limit": "50",
    }
    try:
        resp = requests.get(url, params=params, auth=(user, password), timeout=15)
        resp.raise_for_status()
        return [r["number"] for r in resp.json().get("result", [])]
    except Exception as exc:
        logger.warning("SNOW poll request failed: %s", exc)
        return []


async def _run_sync(ticket_id: str) -> None:
    """Provision a ticket inline (sync / local dev mode)."""
    from .openai_client import load_openai_settings
    from .provisioning_agent import provision_from_ticket

    try:
        settings = load_openai_settings()
        result = await asyncio.to_thread(
            provision_from_ticket,
            openai_settings=settings,
            ticket_id=ticket_id,
        )
        logger.info("Poller: completed ticket=%s pr=%s", ticket_id, result.pr_url)
    except Exception as exc:
        logger.exception("Poller: provisioning failed for ticket=%s: %s", ticket_id, exc)


async def _poll_loop() -> None:
    global _seen_tickets, _initialized

    interval = int(os.getenv("SNOW_POLL_INTERVAL_SECONDS", "30"))

    while True:
        await asyncio.sleep(interval)
        try:
            approved = set(_get_approved_tickets())

            # First run — snapshot current state, don't provision anything
            if not _initialized:
                _seen_tickets = approved
                _initialized = True
                logger.info(
                    "SNOW poller ready — %d existing approved tickets recorded, watching for new ones",
                    len(_seen_tickets),
                )
                continue

            new_tickets = approved - _seen_tickets
            for ticket_id in sorted(new_tickets):
                logger.info("SNOW poller: new approved ticket detected — %s", ticket_id)
                _seen_tickets.add(ticket_id)

                if os.getenv("AZURE_SERVICE_BUS_HOSTNAME"):
                    # Async / production mode — enqueue to Service Bus
                    from .asb_sender import send_provision_message

                    run_id = str(uuid.uuid4())
                    try:
                        from .blob_store import write_run
                        write_run(run_id, {
                            "run_id": run_id,
                            "ticket_id": ticket_id,
                            "status": "queued",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "source": "poller",
                        })
                    except Exception:
                        pass
                    send_provision_message(run_id, ticket_id)
                    logger.info("Poller: queued run_id=%s for ticket=%s", run_id, ticket_id)
                else:
                    # Sync / local dev mode — run inline in background
                    asyncio.create_task(_run_sync(ticket_id))

        except Exception as exc:
            logger.exception("SNOW poller error: %s", exc)


def start_poller() -> None:
    """Start the background polling loop. Call from FastAPI startup."""
    if os.getenv("SNOW_POLL_ENABLED", "true").lower() == "false":
        logger.info("SNOW poller disabled via SNOW_POLL_ENABLED=false")
        return

    instance = os.getenv("SERVICENOW_INSTANCE_URL", "")
    if not instance:
        logger.info("SNOW poller disabled — SERVICENOW_INSTANCE_URL not set")
        return

    interval = int(os.getenv("SNOW_POLL_INTERVAL_SECONDS", "30"))
    logger.info("SNOW poller starting — polling every %ds for approved tickets", interval)
    asyncio.create_task(_poll_loop())
