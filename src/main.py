from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

logging.basicConfig(level=logging.INFO)
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .openai_client import OpenAIClientError, load_openai_settings
from .provisioning_agent import ProvisioningError, provision_from_ticket
from .telemetry import setup_azure_monitor, track_provision_run

app = FastAPI(
    title="Snow → Terraform Provisioning Agent",
    version="0.2.0",
    description=(
        "End-to-end automation: read a ServiceNow ticket, generate Terraform, "
        "open a GitHub PR, and update the ticket — all orchestrated via MCP."
    ),
)


@app.on_event("startup")
async def startup():
    enabled = setup_azure_monitor(app)
    if enabled:
        print("Azure Monitor telemetry enabled")

    from .poller import start_poller
    start_poller()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ProvisionRequest(BaseModel):
    ticket_id: str
    max_iterations: int = 15


class ToolCallResponse(BaseModel):
    name: str
    arguments: Dict[str, Any]
    result_preview: str


class ProvisionResponse(BaseModel):
    """Returned by the synchronous fallback path (no ASB configured)."""
    ticket_id: str
    pr_url: str
    summary: str
    ticket_updated: bool
    iterations: int
    tool_calls: List[ToolCallResponse]
    eval_scores: Optional[Dict[str, Any]] = None
    blocked: bool = False
    blocked_reason: str = ""


class ProvisionAcceptedResponse(BaseModel):
    """Returned immediately (202) when ASB async mode is active."""
    run_id: str
    ticket_id: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asb_enabled() -> bool:
    return bool(os.getenv("AZURE_SERVICE_BUS_HOSTNAME"))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    ui_path = Path(__file__).resolve().parent / "ui.html"
    return ui_path.read_text(encoding="utf-8")


@app.post("/api/provision")
async def api_provision(request: ProvisionRequest, response: Response):
    """Submit a provisioning request.

    - **Async mode** (ASB configured): returns 202 with {run_id}.
      Poll GET /api/provision/{run_id}/status for progress.
    - **Sync fallback** (local dev, no ASB): blocks up to 300 s, returns 200 with full result.
    """
    if not request.ticket_id.strip():
        raise HTTPException(status_code=400, detail="ticket_id is required")
    if request.max_iterations < 1 or request.max_iterations > 20:
        raise HTTPException(status_code=400, detail="max_iterations must be between 1 and 20")

    ticket_id = request.ticket_id.strip()

    # Register with the poller so it doesn't also process this ticket
    from .poller import _seen_tickets
    _seen_tickets.add(ticket_id)

    # ── Async mode: enqueue to Service Bus ───────────────────────────────────
    if _asb_enabled():
        from .asb_sender import send_provision_message

        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        try:
            from .blob_store import write_run
            write_run(run_id, {
                "run_id": run_id,
                "ticket_id": ticket_id,
                "status": "queued",
                "created_at": now,
                "max_iterations": request.max_iterations,
            })
        except Exception as _blob_exc:
            import logging as _log
            _log.getLogger(__name__).warning("Blob write failed (non-fatal): %s", _blob_exc)
        send_provision_message(run_id, ticket_id, request.max_iterations)
        response.status_code = 202
        return ProvisionAcceptedResponse(
            run_id=run_id,
            ticket_id=ticket_id,
            status="queued",
            message=(
                f"Provisioning request queued. "
                f"Poll GET /api/provision/{run_id}/status for updates."
            ),
        )

    # ── Sync fallback: run inline (local dev) ────────────────────────────────
    started_at = time.monotonic()
    try:
        settings = load_openai_settings()
        result = await asyncio.wait_for(
            provision_from_ticket(
                openai_settings=settings,
                ticket_id=ticket_id,
                max_iterations=request.max_iterations,
            ),
            timeout=600,
        )
        track_provision_run(
            ticket_id=ticket_id,
            success=True,
            iterations=result.iterations,
            pr_url=result.pr_url,
            ticket_updated=result.ticket_updated,
            duration_seconds=time.monotonic() - started_at,
        )
        return ProvisionResponse(
            ticket_id=ticket_id,
            pr_url=result.pr_url,
            summary=result.summary,
            ticket_updated=result.ticket_updated,
            iterations=result.iterations,
            tool_calls=[
                ToolCallResponse(
                    name=t.name,
                    arguments=t.arguments,
                    result_preview=t.result_preview,
                )
                for t in result.tool_calls
            ],
            eval_scores=result.eval_scores,
            blocked=result.blocked,
            blocked_reason=result.blocked_reason,
        )
    except asyncio.TimeoutError as exc:
        track_provision_run(ticket_id=ticket_id, success=False, iterations=0,
                            error="timeout", duration_seconds=time.monotonic() - started_at)
        raise HTTPException(
            status_code=504,
            detail=(
                "Provisioning timed out after 300s. "
                "Verify MCP server commands and Azure/SNOW/GitHub credentials."
            ),
        ) from exc
    except ProvisioningError as exc:
        track_provision_run(ticket_id=ticket_id, success=False, iterations=0,
                            error=str(exc), duration_seconds=time.monotonic() - started_at)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OpenAIClientError as exc:
        track_provision_run(ticket_id=ticket_id, success=False, iterations=0,
                            error=str(exc), duration_seconds=time.monotonic() - started_at)
        message = str(exc)
        if "does not match resource tenant" in message.lower():
            raise HTTPException(
                status_code=400,
                detail=(
                    "Azure OpenAI tenant mismatch. Either set AZURE_OPENAI_API_KEY and "
                    "AZURE_OPENAI_USE_AZURE_AD=false, or login to the tenant that owns "
                    "your AZURE_OPENAI_ENDPOINT resource."
                ),
            ) from exc
        raise HTTPException(status_code=400, detail=message) from exc
    except Exception as exc:
        track_provision_run(ticket_id=ticket_id, success=False, iterations=0,
                            error=str(exc), duration_seconds=time.monotonic() - started_at)
        raise HTTPException(status_code=500, detail=f"Unexpected error: {exc}") from exc


@app.get("/api/provision/{run_id}/status")
async def api_provision_status(run_id: str):
    """Poll the status of an async provisioning run.

    Returns the full run blob from Blob Storage, including eval_scores when available.
    Requires AZURE_STORAGE_ACCOUNT_NAME to be configured.
    """
    if not os.getenv("AZURE_STORAGE_ACCOUNT_NAME"):
        raise HTTPException(
            status_code=501,
            detail="Status polling requires AZURE_STORAGE_ACCOUNT_NAME (async mode only)",
        )

    from .blob_store import read_run

    run_data = read_run(run_id)
    if run_data is None:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")

    return JSONResponse(content=run_data)
