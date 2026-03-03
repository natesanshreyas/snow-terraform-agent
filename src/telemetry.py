"""Foundry-native telemetry for the provisioning agent.

Bootstrapping flow:
1. Read AZURE_AI_FOUNDRY_PROJECT_CONNECTION_STRING
   (format: <host>;<subscription_id>;<resource_group>;<project_name>)
2. Use AIProjectClient to pull the App Insights connection string that is
   linked to the Foundry project — no separate APPLICATIONINSIGHTS_* env var needed.
3. Call configure_azure_monitor() with that connection string; FastAPI requests
   are auto-instrumented.
4. All custom events (provision runs, tool calls, LLM iterations) are emitted
   as OpenTelemetry spans so they show up in Foundry Tracing + App Insights.

Fallback: if APPLICATIONINSIGHTS_CONNECTION_STRING is set directly (local dev),
we use it as-is without needing a Foundry project.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Connection string resolution ─────────────────────────────────────────────

_ai_connection_string: Optional[str] = None
_foundry_project_scope: Optional[dict] = None  # {subscription_id, resource_group_name, project_name}


def _resolve_connection_string() -> Optional[str]:
    """Return App Insights connection string, preferring Foundry project resolution."""
    global _ai_connection_string, _foundry_project_scope

    if _ai_connection_string is not None:
        return _ai_connection_string or None

    # 1. Try Foundry project connection string (preferred)
    proj_conn = os.getenv("AZURE_AI_FOUNDRY_PROJECT_CONNECTION_STRING", "")
    if proj_conn:
        try:
            from azure.ai.projects import AIProjectClient
            from azure.identity import DefaultAzureCredential

            client = AIProjectClient.from_connection_string(
                conn_str=proj_conn,
                credential=DefaultAzureCredential(),
            )
            conn_str = client.telemetry.get_connection_string()
            # Stash scope for evaluation logging
            _foundry_project_scope = client.scope
            _ai_connection_string = conn_str or ""
            return conn_str or None
        except Exception as exc:
            print(f"[telemetry] Foundry project init failed: {exc}")

    # 2. Fall back to direct App Insights connection string (local dev)
    direct = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    _ai_connection_string = direct
    return direct or None


def get_foundry_scope() -> Optional[dict]:
    """Return the Foundry project scope dict for azure-ai-evaluation, or None."""
    _resolve_connection_string()  # ensure scope is populated
    return _foundry_project_scope


# ── FastAPI auto-instrumentation ──────────────────────────────────────────────


def setup_azure_monitor(app) -> bool:  # noqa: ANN001
    """Call once at FastAPI startup. Returns True if telemetry was configured."""
    conn_str = _resolve_connection_string()
    if not conn_str:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        configure_azure_monitor(connection_string=conn_str)
        FastAPIInstrumentor.instrument_app(app)
        return True
    except Exception as exc:
        print(f"[telemetry] Azure Monitor setup failed: {exc}")
        return False


# ── OpenTelemetry span helpers (replace applicationinsights custom events) ────


def _get_tracer():
    try:
        from opentelemetry import trace
        return trace.get_tracer("snow-tf-agent")
    except Exception:
        return None


def track_provision_run(
    ticket_id: str,
    success: bool,
    iterations: int,
    pr_url: str = "",
    ticket_updated: bool = False,
    error: str = "",
    duration_seconds: float = 0.0,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    try:
        with tracer.start_as_current_span("provision_run") as span:
            span.set_attribute("ticket_id", ticket_id)
            span.set_attribute("success", success)
            span.set_attribute("pr_url", pr_url)
            span.set_attribute("ticket_updated", ticket_updated)
            span.set_attribute("iterations", iterations)
            span.set_attribute("duration_seconds", duration_seconds)
            if error:
                span.set_attribute("error", error[:500])
    except Exception:
        pass


def track_tool_call(
    tool_name: str,
    ticket_id: str,
    duration_seconds: float,
    success: bool,
    error: str = "",
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    try:
        server = tool_name.split("__")[0] if "__" in tool_name else "internal"
        with tracer.start_as_current_span("mcp_tool_call") as span:
            span.set_attribute("tool_name", tool_name)
            span.set_attribute("server", server)
            span.set_attribute("ticket_id", ticket_id)
            span.set_attribute("success", success)
            span.set_attribute("duration_seconds", duration_seconds)
            if error:
                span.set_attribute("error", error[:300])
    except Exception:
        pass


def track_llm_call(
    ticket_id: str,
    iteration: int,
    action_returned: str,
    duration_seconds: float,
) -> None:
    tracer = _get_tracer()
    if tracer is None:
        return
    try:
        with tracer.start_as_current_span("llm_iteration") as span:
            span.set_attribute("ticket_id", ticket_id)
            span.set_attribute("iteration", iteration)
            span.set_attribute("action_returned", action_returned)
            span.set_attribute("duration_seconds", duration_seconds)
    except Exception:
        pass


# ── Timer utility (unchanged) ─────────────────────────────────────────────────


class Timer:
    """Simple context manager for timing a block."""

    def __init__(self):
        self.elapsed = 0.0

    def __enter__(self):
        self._start = time.monotonic()
        return self

    def __exit__(self, *_):
        self.elapsed = time.monotonic() - self._start
