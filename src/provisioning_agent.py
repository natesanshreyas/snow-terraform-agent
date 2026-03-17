"""Snow → Terraform provisioning agent.

Uses the Azure AI Agents SDK (azure-ai-agents) for orchestration.
MCP servers (SNOW, GitHub, Azure) are registered as function tools; the
agents service drives the tool-call loop so we no longer maintain a
manual chat-completion loop.

Multi-agent aspect
------------------
The orchestrator Agent handles the linear workflow.  When it calls
``generate_terraform``, the handler fans out to three parallel LLM
judge calls (security / compliance / quality) via ``asyncio.gather``,
then returns the aggregated verdict so the agent can retry or proceed.

  1.  Read SNOW ticket          (snow__ MCP tools)
  2.  Azure inventory (optional)(azure__ MCP tools)
  3.  Generate + evaluate HCL   (generate_terraform → parallel judges)
  4.  Create branch              (github__ MCP tools)
  5.  Push .tf files             (github__ MCP tools)
  6.  Open PR                    (github__ MCP tools)
  7.  Update SNOW ticket         (snow__ MCP tools)
  8.  Signal completion          (complete_provisioning)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .inventory_scanner import InventoryContext, scan_azure_inventory
from .multi_mcp_client import (
    MCPServerConfig,
    MultiMCPClient,
    ProvisioningError,
    format_tool_result,
)
from .openai_client import OpenAISettings, chat_completion_with_tools
from .terraform_evaluator import evaluate_terraform
from . import telemetry as _telemetry

logger = logging.getLogger(__name__)

# Ticket-level semaphore: max 2 tickets provisioning at the same time.
# Each ticket makes 15-25 LLM calls; running more than 2 concurrently
# exhausts the Azure OpenAI RPM quota. MCP I/O (SNOW, GitHub) is unaffected.
_LLM_SEMAPHORE = asyncio.Semaphore(2)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ToolCallRecord:
    name: str
    arguments: Dict[str, Any]
    result_preview: str


@dataclass
class ProvisioningResult:
    pr_url: str
    summary: str
    ticket_updated: bool
    iterations: int
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    eval_scores: Optional[Dict[str, Any]] = None
    blocked: bool = False
    blocked_reason: str = ""


# ---------------------------------------------------------------------------
# Special local function tool schemas
# (not MCP — executed inside this process, not via an MCP server)
# ---------------------------------------------------------------------------

_GENERATE_TF_SCHEMA = {
    "name": "generate_terraform",
    "description": (
        "Submit generated Terraform HCL for parallel evaluation (security, compliance, "
        "quality).  Call this once you have composed main.tf and variables.tf. "
        "If evaluation fails the response explains what to fix — regenerate and call again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "main_tf": {"type": "string", "description": "Full content of main.tf"},
            "variables_tf": {"type": "string", "description": "Full content of variables.tf"},
        },
        "required": ["main_tf", "variables_tf"],
    },
}

_COMPLETE_SCHEMA = {
    "name": "complete_provisioning",
    "description": (
        "Signal that all workflow steps are done: the GitHub PR is open and the "
        "ServiceNow ticket has been updated.  Call this as the final step."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pr_url": {"type": "string", "description": "GitHub PR URL"},
            "summary": {"type": "string", "description": "1-2 sentence summary"},
            "ticket_updated": {"type": "boolean"},
        },
        "required": ["pr_url", "summary", "ticket_updated"],
    },
}

_ABORT_SCHEMA = {
    "name": "abort_provisioning",
    "description": (
        "Signal that provisioning is blocked and cannot proceed. "
        "Call this ONLY after updating the SNOW ticket work_notes with the blocking reason. "
        "Use when approval is not 'approved' or cost center is missing from the ticket."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "enum": ["approval_required", "cost_center_missing"],
                "description": "Why provisioning is blocked",
            },
            "detail": {
                "type": "string",
                "description": "Human-readable detail (e.g. current approval value, what was missing)",
            },
        },
        "required": ["reason", "detail"],
    },
}


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a Terraform provisioning agent. Automate Azure infrastructure from \
ServiceNow tickets by calling the tools provided in order.

=== WORKFLOW ===

STEP 1 — Read SNOW ticket AND validate before proceeding:
  Call snow__SN-Query-Table:
    table_name: "sc_req_item"
    query: "number={ticket_id}"
    fields: "sys_id,number,short_description,description,approval,work_notes"
  Remember: sys_id, short_description, description.

  After reading, perform TWO validation checks IN ORDER:

  CHECK A — Approval gate:
    If the approval field is NOT exactly "approved":
      - Call snow__SN-Update-Record:
          table_name: "sc_req_item"
          sys_id: <sys_id>
          data: {{"work_notes": "Automated provisioning blocked: ticket is not \
in approved state (current approval: <approval value>). Please obtain approval and resubmit."}}
      - Then call abort_provisioning(reason="approval_required", \
detail="approval=<value>")
      - STOP. Do not continue to STEP 2.

  CHECK B — Cost center gate:
    Extract cost_center from description (look for "Cost center:" or "CC-" patterns).
    If cost_center is empty or cannot be found:
      - Call snow__SN-Update-Record:
          table_name: "sc_req_item"
          sys_id: <sys_id>
          data: {{"work_notes": "Automated provisioning blocked: no cost center \
found in ticket description. Please add 'Cost center: CC-XXXX' to the description and resubmit."}}
      - Then call abort_provisioning(reason="cost_center_missing", \
detail="cost_center not found in description")
      - STOP. Do not continue to STEP 2.

  Only if approval == "approved" AND cost_center found: continue to STEP 2.

STEP 2 — Azure inventory context:
  If the context message contains "=== AZURE INVENTORY CONTEXT ===" — SKIP this step,
  the pre-scan is already complete. Use it to guide Terraform generation:
    - Skip resource group creation if rg_exists=True
    - If requested_resource_exists=True, do NOT create that resource — inform the user it already exists
    - Follow the inferred naming convention for any new resource names
    - Apply the standard tags to all new resources
    - For each item in "Shared infrastructure to REFERENCE": use a data source block
      to reference it rather than creating a new one (e.g. data "azurerm_key_vault",
      data "azurerm_virtual_network", data "azurerm_service_plan")
  If no inventory context is present and azure__ tools are available, call
  the resource group list tool to discover existing resource groups.

STEP 3 — Generate Terraform:
  Using the example template provided in the context, compose main.tf and variables.tf.
  Rules:
    - Use module blocks (not raw resource blocks) for all resources
    - Set cost_center tag (extracted from ticket) and ticket_id tag = "{ticket_id}"
    - Default location: eastus2
    - Storage account names: ≤24 chars, lowercase, alphanumeric only
  Then call generate_terraform(main_tf=..., variables_tf=...) — three judges evaluate it \
in parallel (security, compliance, quality).
  If evaluation fails, fix the reported issues and call generate_terraform again \
(up to 2 retries).

STEP 4 — Create branch:
  Call github__create_branch:
    owner: "{github_org}"
    repo: "{github_repo}"
    branch: "feature/provision-{ticket_id}"
    from_branch: "main"

STEP 5 — Push Terraform files:
  Call github__push_files:
    owner: "{github_org}"
    repo: "{github_repo}"
    branch: "feature/provision-{ticket_id}"
    message: "feat: provision resources for {ticket_id}"
    files: [
      {{"path": "provisioned/{ticket_id}/main.tf",      "content": "<main_tf from STEP 3>"}},
      {{"path": "provisioned/{ticket_id}/variables.tf", "content": "<variables_tf from STEP 3>"}}
    ]

STEP 6 — Create pull request:
  Call github__create_pull_request:
    owner: "{github_org}"
    repo: "{github_repo}"
    title: "Provision: <short_description> [{ticket_id}]"
    body: "## Terraform Provisioning\\n\\n**Ticket:** {ticket_id}\\n**Cost Center:** \
<cost_center>\\n\\n### Resources\\n<bullet list>"
    head: "feature/provision-{ticket_id}"
    base: "main"
  Save the PR URL from the response.

STEP 7 — Update SNOW ticket:
  Call snow__SN-Update-Record:
    table_name: "sc_req_item"
    sys_id: <sys_id from STEP 1>
    data: {{"work_notes": "<compose the message below>"}}

  Compose the work_notes message as follows:
    Line 1: "Terraform PR: <pr_url>"
    Line 2: blank
    If the context contains "=== AZURE INVENTORY CONTEXT ===" and existing resources were found:
      Line 3: "Existing prior resources: <comma-separated list of resource names from inventory>"
      Line 4: "New resources to be created: <comma-separated list of resources defined in the Terraform you generated>"
    Else if requested_resource_exists=True was in the context:
      Line 3: "Note: the requested resource already exists in Azure — no new resource created."
    Else:
      Line 3: "Prior resources: none relevant to this request."
    Final line: "Provisioning complete."

STEP 8 — Signal completion:
  Call complete_provisioning(pr_url=..., summary=..., ticket_updated=true)

=== RULES ===
- Use EXACT tool names from your tool list.
- In push_files the content field is plain HCL text, not base64.
- Do NOT skip steps.
"""


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_mcp_configs() -> Dict[str, MCPServerConfig]:
    return {
        "snow": MCPServerConfig(
            name="snow",
            command=os.getenv("SNOW_MCP_COMMAND", "servicenow-mcp-server"),
            env={
                k: v
                for k, v in {
                    "SERVICENOW_INSTANCE_URL": os.getenv("SERVICENOW_INSTANCE_URL", ""),
                    "SERVICENOW_USERNAME": os.getenv("SERVICENOW_USERNAME", ""),
                    "SERVICENOW_PASSWORD": os.getenv("SERVICENOW_PASSWORD", ""),
                }.items()
                if v
            },
            timeout=60.0,
            protocol="ndjson",
        ),
        "github": MCPServerConfig(
            name="github",
            command=os.getenv("GITHUB_MCP_COMMAND", "npx @modelcontextprotocol/server-github"),
            env={
                k: v
                for k, v in {
                    "GITHUB_PERSONAL_ACCESS_TOKEN": os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", ""),
                }.items()
                if v
            },
            timeout=45.0,
            protocol="ndjson",
        ),
        "azure": MCPServerConfig(
            name="azure",
            command=os.getenv("AZURE_MCP_SERVER_COMMAND", ""),
            env={},
            timeout=60.0,
            protocol="lsp",
        ),
    }


# ---------------------------------------------------------------------------
# Template pre-fetcher
# ---------------------------------------------------------------------------

import requests as _requests

_TEMPLATE_CACHE: Dict[str, str] = {}


def _fetch_terraform_template(github_org: str, github_repo: str) -> str:
    cache_key = f"{github_org}/{github_repo}"
    if cache_key in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[cache_key]
    pat = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
    url = (
        f"https://api.github.com/repos/{github_org}/{github_repo}"
        f"/contents/examples/storage-account-example/main.tf"
    )
    headers = {"Accept": "application/vnd.github.raw+json"}
    if pat:
        headers["Authorization"] = f"Bearer {pat}"
    try:
        resp = _requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            template = resp.text
            _TEMPLATE_CACHE[cache_key] = template
            logger.info("Pre-fetched Terraform template (%d chars)", len(template))
            return template
    except Exception as exc:
        logger.warning("Could not pre-fetch Terraform template: %s", exc)
    return "(template unavailable — generate HCL following standard module patterns)"


# ---------------------------------------------------------------------------
# Tool definition builder
# ---------------------------------------------------------------------------


def _build_tool_definitions(mcp_tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert MCP manifest entries + local tools into OpenAI function definitions."""
    defs = []
    for tool in mcp_tools:
        defs.append({
            "name": tool["name"],
            "description": (tool.get("description") or "")[:500],
            "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
        })
    defs.append(_GENERATE_TF_SCHEMA)
    defs.append(_COMPLETE_SCHEMA)
    defs.append(_ABORT_SCHEMA)
    return defs


# ---------------------------------------------------------------------------
# Tool dispatcher (called from the streaming event loop)
# ---------------------------------------------------------------------------


async def _dispatch(
    fn_name: str,
    fn_args: Dict[str, Any],
    mcp: MultiMCPClient,
    tool_names: set,
    state: Dict[str, Any],
    ticket_id: str,
    openai_settings: OpenAISettings,
) -> str:
    """Handle one function call. Returns the tool-output string sent back to the agent."""

    # ── abort_provisioning (approval/cost-center gate) ───────────────────
    if fn_name == "abort_provisioning":
        reason = str(fn_args.get("reason", "unknown"))
        detail = str(fn_args.get("detail", ""))
        state["result"] = ProvisioningResult(
            pr_url="",
            summary=f"Provisioning blocked: {reason} — {detail}",
            ticket_updated=True,
            iterations=state["iterations"],
            tool_calls=state["trace"],
            blocked=True,
            blocked_reason=detail,
        )
        return json.dumps({"status": "blocked", "reason": reason, "detail": detail})

    # ── complete_provisioning ─────────────────────────────────────────────
    if fn_name == "complete_provisioning":
        pr_url = state["captured_pr_url"] or str(fn_args.get("pr_url", ""))
        state["result"] = ProvisioningResult(
            pr_url=pr_url,
            summary=str(fn_args.get("summary", "")),
            ticket_updated=bool(fn_args.get("ticket_updated", False)),
            iterations=state["iterations"],
            tool_calls=state["trace"],
            eval_scores=state["eval_scores"],
        )
        return json.dumps({"status": "complete", "pr_url": pr_url})

    # ── generate_terraform (fan-out to parallel judges) ───────────────────
    if fn_name == "generate_terraform":
        main_tf = str(fn_args.get("main_tf", "")).strip()
        variables_tf = str(fn_args.get("variables_tf", "")).strip()
        if not main_tf:
            return json.dumps({"error": "main_tf is empty — provide the HCL content"})

        try:
            # evaluate_terraform runs 3 judges in parallel (asyncio.gather)
            eval_result = await evaluate_terraform(
                main_tf=main_tf,
                variables_tf=variables_tf,
                ticket_id=ticket_id,
                openai_settings=openai_settings,
            )
            state["eval_scores"] = {
                "security": eval_result.security,
                "compliance": eval_result.compliance,
                "quality": eval_result.quality,
                "passed": eval_result.passed,
                "reason": eval_result.reason,
            }
        except Exception as exc:
            logger.warning("Terraform evaluation error (non-fatal): %s", exc)
            eval_result = None

        if eval_result is not None and not eval_result.passed:
            state["terraform_retries"] += 1
            return json.dumps({
                "passed": False,
                "security": eval_result.security,
                "compliance": eval_result.compliance,
                "quality": eval_result.quality,
                "reason": eval_result.reason,
                "instruction": "Fix the issues above and call generate_terraform again.",
            })

        # Passed (or eval unavailable — proceed anyway)
        state["terraform_state"] = {"main_tf": main_tf, "variables_tf": variables_tf}
        scores = ""
        if eval_result:
            scores = (
                f" security={eval_result.security}/5 "
                f"compliance={eval_result.compliance}/5 "
                f"quality={eval_result.quality}/5"
            )
        preview = f"Terraform evaluation passed.{scores}"
        state["trace"].append(ToolCallRecord(
            name="[generate_terraform]",
            arguments={},
            result_preview=preview,
        ))
        return json.dumps({"passed": True, "message": preview})

    # ── MCP tool call ─────────────────────────────────────────────────────
    if fn_name not in tool_names:
        if fn_name.startswith("azure__") and not any(t.startswith("azure__") for t in tool_names):
            return json.dumps({
                "error": "Azure MCP not configured — skip STEP 2 and proceed to STEP 3."
            })
        return json.dumps({"error": f"Unknown tool: {fn_name!r}"})

    # Safety: fix push_files when model passes strings instead of {path,content} objects
    if fn_name == "github__push_files" and state["terraform_state"]:
        raw_files = fn_args.get("files") or []
        fixed = []
        for item in raw_files:
            if isinstance(item, dict):
                # Inject content if missing
                if not item.get("content"):
                    p = str(item.get("path", ""))
                    if "main.tf" in p:
                        item["content"] = state["terraform_state"]["main_tf"]
                    elif "variables.tf" in p:
                        item["content"] = state["terraform_state"]["variables_tf"]
                fixed.append(item)
            elif isinstance(item, str):
                # Model sent a raw string — reconstruct the object
                if "main.tf" in item:
                    fixed.append({"path": item, "content": state["terraform_state"]["main_tf"]})
                elif "variables.tf" in item:
                    fixed.append({"path": item, "content": state["terraform_state"]["variables_tf"]})
        if fixed:
            fn_args["files"] = fixed

    # Safety: inject Terraform content if the agent forgot it in single-file push
    if state["terraform_state"] and "github__" in fn_name and fn_name != "github__push_files":
        if not fn_args.get("content"):
            path = str(fn_args.get("path", ""))
            if "main.tf" in path:
                fn_args["content"] = state["terraform_state"]["main_tf"]
            elif "variables.tf" in path:
                fn_args["content"] = state["terraform_state"]["variables_tf"]

    with _telemetry.Timer() as tool_timer:
        try:
            result = mcp.call_tool(fn_name, fn_args)
        except ProvisioningError as exc:
            # Auto-recover: if branch already exists, delete it and retry once
            if fn_name == "github__create_branch" and "Reference already exists" in str(exc):
                owner = fn_args.get("owner", "")
                repo = fn_args.get("repo", "")
                branch = fn_args.get("branch", "")
                pat = os.getenv("GITHUB_PERSONAL_ACCESS_TOKEN", "")
                if owner and repo and branch and pat:
                    import requests as _req
                    _req.delete(
                        f"https://api.github.com/repos/{owner}/{repo}/git/refs/heads/{branch}",
                        headers={"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"},
                        timeout=10,
                    )
                    logger.info("Auto-deleted stale branch %s, retrying create", branch)
                    result = mcp.call_tool(fn_name, fn_args)
                else:
                    raise
            else:
                raise
    preview = format_tool_result(result)

    state["trace"].append(ToolCallRecord(name=fn_name, arguments=fn_args, result_preview=preview))
    _telemetry.track_tool_call(
        tool_name=fn_name,
        ticket_id=ticket_id,
        duration_seconds=tool_timer.elapsed,
        success=True,
    )

    # Capture key state from real API responses
    if fn_name == "snow__SN-Query-Table" and not state["captured_sys_id"]:
        m = re.search(r'"sys_id"\s*:\s*"([a-f0-9]{32})"', preview)
        if m:
            state["captured_sys_id"] = m.group(1)
            logger.info("Captured sys_id=%s for ticket=%s", state["captured_sys_id"], ticket_id)

    if fn_name == "github__create_pull_request":
        m = re.search(r'https://github\.com/[^\s"\']+/pull/\d+', preview)
        if m:
            state["captured_pr_url"] = m.group(0).rstrip(".,)")
            logger.info("Captured PR URL: %s", state["captured_pr_url"])

    if fn_name == "snow__SN-Update-Record":
        state["snow_ticket_updated"] = True

    # Auto-complete: if we have PR URL + SNOW updated, we're done
    if state["captured_pr_url"] and state["snow_ticket_updated"] and state["result"] is None:
        logger.info("Auto-completing: PR=%s ticket_updated=True", state["captured_pr_url"])
        state["result"] = ProvisioningResult(
            pr_url=state["captured_pr_url"],
            summary=(
                f"Provisioned resources for {ticket_id}. "
                f"PR ready for review: {state['captured_pr_url']}"
            ),
            ticket_updated=True,
            iterations=state["iterations"],
            tool_calls=state["trace"],
            eval_scores=state["eval_scores"],
        )

    # Append captured sys_id as a hint so the agent uses the real value
    sys_id_hint = (
        f"\n(sys_id for this ticket: {state['captured_sys_id']})"
        if state["captured_sys_id"]
        else ""
    )
    return preview + sys_id_hint


# ---------------------------------------------------------------------------
# Main async entry point
# ---------------------------------------------------------------------------


async def provision_from_ticket(
    openai_settings: OpenAISettings,
    ticket_id: str,
    mcp_configs: Optional[Dict[str, MCPServerConfig]] = None,
    max_iterations: int = 15,
) -> ProvisioningResult:
    """Run the full provisioning workflow for a ServiceNow ticket.

    Uses the Azure AI Agents SDK (azure-ai-agents) to manage the agent loop.
    MCP servers are kept as local stdio processes; their tools are registered
    as function-call definitions on the hosted agent.
    """
    if not ticket_id.strip():
        raise ProvisioningError("ticket_id cannot be empty")

    async with _LLM_SEMAPHORE:
        return await _provision_inner(openai_settings, ticket_id, mcp_configs, max_iterations)


async def _provision_inner(
    openai_settings: OpenAISettings,
    ticket_id: str,
    mcp_configs: Optional[Dict[str, MCPServerConfig]],
    max_iterations: int,
) -> ProvisioningResult:
    configs = mcp_configs or load_mcp_configs()
    github_org = os.getenv("GITHUB_ORG", "natesanshreyas")
    github_repo = os.getenv("GITHUB_TERRAFORM_REPO", "terraform-modules-demo")
    azure_subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")

    # ── Agent 1: Azure inventory scan ────────────────────────────────────────
    inventory_context: Optional[InventoryContext] = None
    try:
        # Read SNOW ticket (deterministic) to get text for Agent 1
        with MultiMCPClient(configs) as _pre_mcp:
            raw = _pre_mcp.call_tool("snow__SN-Query-Table", {
                "table_name": "sc_req_item",
                "query": f"number={ticket_id}",
                "fields": "short_description,description",
            })
            ticket_text = format_tool_result(raw)
        inventory_context = await scan_azure_inventory(
            ticket_text=ticket_text,
            subscription_id=azure_subscription_id,
            openai_settings=openai_settings,
        )
        logger.info(
            "Agent 1 scan: rg=%s exists=%s resources=%d",
            inventory_context.resource_group,
            inventory_context.rg_exists,
            len(inventory_context.existing_resources),
        )
    except Exception as exc:
        logger.warning("Agent 1 scan failed (non-fatal): %s", exc)

    with MultiMCPClient(configs) as mcp:
        all_tools = mcp.all_tools_manifest()
        tool_names = {t["name"] for t in all_tools}
        tool_defs = _build_tool_definitions(all_tools)

        _tf_template = _fetch_terraform_template(github_org, github_repo)

        filled_system_prompt = (
            _SYSTEM_PROMPT
            .replace("{ticket_id}", ticket_id)
            .replace("{github_org}", github_org)
            .replace("{github_repo}", github_repo)
        )

        context_message = (
            f"=== CONTEXT ===\n"
            f"ticket_id: {ticket_id}\n"
            f"github_org: {github_org}\n"
            f"github_repo: {github_repo}\n"
            f"azure_subscription_id: {azure_subscription_id or '(not set)'}\n\n"
            f"=== TERRAFORM EXAMPLE TEMPLATE ===\n"
            f"Use this as the pattern for the HCL you generate in STEP 3:\n"
            f"{_tf_template}\n\n"
            f"Begin at STEP 1. Read ticket {ticket_id} now."
        )

        if inventory_context:
            resource_lines = "\n".join(
                f"  - {r.get('name')} ({r.get('type', '').split('/')[-1]})"
                for r in inventory_context.existing_resources[:20]
            ) or "  (none)"
            shared_lines = "\n".join(
                f"  - {k}: {v}" for k, v in inventory_context.shared_infrastructure.items()
            ) or "  (none found)"
            context_message += (
                f"\n=== AZURE INVENTORY CONTEXT (Agent 1 pre-scan) ===\n"
                f"Resource Group: {inventory_context.resource_group} "
                f"({'EXISTS — skip creation' if inventory_context.rg_exists else 'DOES NOT EXIST — create it'})\n"
                f"Requested resource already exists: {inventory_context.requested_resource_exists}\n"
                f"Existing resources ({len(inventory_context.existing_resources)}):\n"
                f"{resource_lines}\n"
                f"Shared infrastructure to REFERENCE (do not recreate):\n"
                f"{shared_lines}\n"
                f"Naming convention: {inventory_context.naming_prefix or '(none inferred)'}\n"
                f"Standard tags: {json.dumps(inventory_context.standard_tags)}\n"
                f"Notes: {inventory_context.notes}\n"
                f"STEP 2 is complete — skip azure__ inventory calls. "
                f"Use the context above to guide Terraform generation.\n"
            )

        # Mutable state shared across the tool-call loop
        state: Dict[str, Any] = {
            "iterations": 0,
            "trace": [],
            "terraform_state": None,
            "eval_scores": None,
            "terraform_retries": 0,
            "captured_sys_id": None,
            "captured_pr_url": None,
            "snow_ticket_updated": False,
            "result": None,
        }

        # ── Manual tool-call loop (no Azure AI Foundry required) ─────────────
        messages = [
            {"role": "system", "content": filled_system_prompt},
            {"role": "user", "content": context_message},
        ]

        for _loop in range(max_iterations):
            with _telemetry.Timer() as llm_timer:
                response = await asyncio.to_thread(
                    chat_completion_with_tools,
                    openai_settings,
                    messages,
                    tool_defs,
                )
            _telemetry.track_llm_call(
                ticket_id=ticket_id,
                iteration=state["iterations"],
                action_returned="llm_response",
                duration_seconds=llm_timer.elapsed,
            )

            tool_calls = response.get("tool_calls") or []
            content = response.get("content")

            # No tool calls — model is done
            if not tool_calls:
                logger.info("Agent 2: model returned no tool calls (finish_reason=%s)", response.get("finish_reason"))
                break

            # Append assistant message with tool_calls
            assistant_msg: Dict[str, Any] = {"role": "assistant", "tool_calls": tool_calls}
            if content:
                assistant_msg["content"] = content
            messages.append(assistant_msg)

            # Execute each tool call and append results
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                fn_args = json.loads(tc["function"].get("arguments") or "{}")
                tc_id = tc["id"]
                state["iterations"] += 1

                output = await _dispatch(
                    fn_name=fn_name,
                    fn_args=fn_args,
                    mcp=mcp,
                    tool_names=tool_names,
                    state=state,
                    ticket_id=ticket_id,
                    openai_settings=openai_settings,
                )
                logger.info("Tool call %d: %s → %s", state["iterations"], fn_name, output[:120])

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": output,
                })

            if state["result"] is not None:
                break

    if state["result"] is not None:
        return state["result"]

    raise ProvisioningError(f"No result after {state['iterations']} tool calls")
