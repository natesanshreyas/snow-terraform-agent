"""Background poller — watches ServiceNow for new active RITM tickets,
auto-approves them, and triggers the provisioning agent.

Behaviour:
- On first poll, snapshots all existing tickets (doesn't re-process them).
- On every subsequent poll, any NEW ticket is automatically approved via
  REST API and then provisioned — no manual approval needed.
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


def _get_all_tickets() -> list[dict]:
    """Returns all active RITM tickets (any approval state)."""
    instance = os.getenv("SERVICENOW_INSTANCE_URL", "").rstrip("/")
    user = os.getenv("SERVICENOW_USERNAME", "")
    password = os.getenv("SERVICENOW_PASSWORD", "")
    if not instance or not user or not password:
        return []

    url = f"{instance}/api/now/table/sc_req_item"
    params = {
        "sysparm_query": "active=true",
        "sysparm_fields": "number,sys_id,short_description,description,comments,approval",
        "sysparm_limit": "50",
    }
    try:
        resp = requests.get(url, params=params, auth=(user, password), timeout=15)
        resp.raise_for_status()
        return resp.json().get("result", [])
    except Exception as exc:
        logger.warning("SNOW poll request failed: %s", exc)
        return []


def _get_journal_text(sys_id: str) -> str:
    """Read the most recent customer-visible comment from the journal table."""
    instance = os.getenv("SERVICENOW_INSTANCE_URL", "").rstrip("/")
    user = os.getenv("SERVICENOW_USERNAME", "")
    password = os.getenv("SERVICENOW_PASSWORD", "")
    try:
        resp = requests.get(
            f"{instance}/api/now/table/sys_journal_field",
            params={
                "sysparm_query": f"element_id={sys_id}^element=comments^ORDERBYDESCsys_created_on",
                "sysparm_fields": "value",
                "sysparm_limit": "1",
            },
            auth=(user, password),
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("result", [])
        return results[0]["value"].strip() if results else ""
    except Exception as exc:
        logger.warning("Poller: could not read journal for %s: %s", sys_id, exc)
        return ""


def _ensure_short_description(ticket: dict) -> None:
    """If short_description is blank, pull from description, comments, or journal."""
    if ticket.get("short_description", "").strip():
        return

    # Try inline fields first, then journal entries
    text = (
        ticket.get("description") or
        ticket.get("comments") or
        _get_journal_text(ticket["sys_id"])
    ).strip()

    if not text:
        return

    instance = os.getenv("SERVICENOW_INSTANCE_URL", "").rstrip("/")
    user = os.getenv("SERVICENOW_USERNAME", "")
    password = os.getenv("SERVICENOW_PASSWORD", "")
    try:
        requests.patch(
            f"{instance}/api/now/table/sc_req_item/{ticket['sys_id']}",
            json={"short_description": text},
            auth=(user, password),
            timeout=15,
        )
        ticket["short_description"] = text
        logger.info("Poller: set short_description for %s: '%s'", ticket["number"], text[:80])
    except Exception as exc:
        logger.warning("Poller: could not set short_description for %s: %s", ticket["number"], exc)


def _approve_ticket(sys_id: str, ticket_number: str) -> bool:
    """Approve a ticket directly via REST API, bypassing the approval engine."""
    instance = os.getenv("SERVICENOW_INSTANCE_URL", "").rstrip("/")
    user = os.getenv("SERVICENOW_USERNAME", "")
    password = os.getenv("SERVICENOW_PASSWORD", "")
    try:
        resp = requests.patch(
            f"{instance}/api/now/table/sc_req_item/{sys_id}",
            json={"approval": "approved"},
            auth=(user, password),
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("Poller: approved ticket %s", ticket_number)
        return True
    except Exception as exc:
        logger.warning("Poller: failed to approve %s: %s", ticket_number, exc)
        return False


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
            tickets = _get_all_tickets()
            all_numbers = {t["number"] for t in tickets}

            # First run — snapshot everything currently in SNOW, don't process
            if not _initialized:
                _seen_tickets = all_numbers
                _initialized = True
                logger.info(
                    "SNOW poller ready — %d existing tickets recorded, watching for new ones",
                    len(_seen_tickets),
                )
                continue

            # Find tickets submitted since we started
            ticket_map = {t["number"]: t for t in tickets}
            new_numbers = all_numbers - _seen_tickets

            for ticket_number in sorted(new_numbers):
                ticket = ticket_map[ticket_number]
                logger.info("SNOW poller: new ticket detected — %s", ticket_number)
                _seen_tickets.add(ticket_number)

                # Copy description/comments → short_description if blank
                _ensure_short_description(ticket)

                # Auto-approve it
                _approve_ticket(ticket["sys_id"], ticket_number)

                # Trigger provisioning
                if os.getenv("AZURE_SERVICE_BUS_HOSTNAME"):
                    from .asb_sender import send_provision_message
                    run_id = str(uuid.uuid4())
                    try:
                        from .blob_store import write_run
                        write_run(run_id, {
                            "run_id": run_id,
                            "ticket_id": ticket_number,
                            "status": "queued",
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "source": "poller",
                        })
                    except Exception:
                        pass
                    send_provision_message(run_id, ticket_number)
                    logger.info("Poller: queued run_id=%s for ticket=%s", run_id, ticket_number)
                else:
                    asyncio.create_task(_run_sync(ticket_number))

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
    logger.info("SNOW poller starting — polling every %ds for new tickets", interval)
    asyncio.create_task(_poll_loop())
