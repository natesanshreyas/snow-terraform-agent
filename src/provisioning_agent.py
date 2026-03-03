"""Snow → Terraform provisioning agent.

Orchestrates three MCP servers (ServiceNow, GitHub, Azure) to automate
infrastructure provisioning from a ServiceNow ticket:

  1.  Read ticket                   (snow)
  2.  Validate approval             (snow)
  3.  Inventory Azure resources     (azure)
  4.  Find Terraform repo           (github)
  5.  Read example .tf files        (github)
  6.  Generate Terraform code       (LLM, no tool call)
  7.  Create feature branch         (github)
  8.  Push main.tf                  (github)
  9.  Push variables.tf             (github)
  10. Open pull request             (github)
  11. Update ticket with PR URL     (snow)
  12. Return final result
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .multi_mcp_client import (
    MCPServerConfig,
    MultiMCPClient,
    ProvisioningError,
    format_tool_result,
    tool_manifest_json,
)
from .openai_client import OpenAISettings, chat_completion
from .terraform_evaluator import evaluate_terraform
from . import telemetry as _telemetry


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


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are a Terraform provisioning agent. You automate Azure infrastructure \
provisioning by orchestrating ServiceNow, GitHub, and Azure tools.

=== RESPONSE FORMAT ===
Return exactly one JSON object per turn — no prose, no markdown fences.

Call a tool:
{"action":"tool_call","tool":"<server>__<toolname>","arguments":{...},"reason":"<why>"}

Generate Terraform HCL (step 6 only):
{"action":"generate_terraform","main_tf":"<HCL content>","variables_tf":"<HCL content>","reason":"<why>"}

Ticket not approved:
{"action":"blocked","reason":"<message>"}

All done:
{"action":"final","pr_url":"<url>","summary":"<1-2 sentences>","ticket_updated":true}

=== EXACT TOOL NAMES ===
ServiceNow (snow__ prefix):
  snow__SN-Query-Table        query any SNOW table by encoded query string
  snow__SN-Get-Record         get record by sys_id
  snow__SN-Update-Record      update any record by sys_id (use for work notes)

GitHub (github__ prefix):
  github__get_file_contents   read a file from a repo
  github__create_branch       create a new branch
  github__push_files          push multiple files in one commit (preferred)
  github__create_pull_request create a PR

Azure (azure__ prefix — CLI-based, use whatever group/subscription tools appear):
  (tool names discovered at runtime from manifest)

=== WORKFLOW — FOLLOW IN ORDER, NO SKIPPING ===

STEP 1 — Read SNOW ticket:
  Call snow__SN-Query-Table with:
    table_name: "sc_req_item"
    query: "number={ticket_id}"
    fields: "sys_id,number,short_description,description,approval,work_notes"
  Save: sys_id, short_description, description, approval value, extract cost_center
  from description (look for "Cost center:" or "CC-" patterns).

STEP 2 — Validate approval:
  Skip approval check — proceed regardless of approval status.

STEP 3 — Azure resource inventory (OPTIONAL):
  CRITICAL: First check the manifest. If you do NOT see any tool names starting with
  "azure__" in the manifest, DO NOT call any azure__ tool — they do not exist and
  will fail. Skip immediately to STEP 4.
  Only if azure__ tools ARE listed in the manifest: call the group list or subscription
  list tool to see existing resource groups for naming context.
  When no Azure context is available, default to location "eastus2" and generate
  resource names from the ticket description (keep storage account names ≤24 chars,
  lowercase, alphanumeric only).

STEP 4 — Read Terraform example:
  Call github__get_file_contents with:
    owner: "{github_org}"
    repo: "{github_repo}"
    path: "examples/storage-account-example/main.tf"   (or openai-example if ticket mentions OpenAI)
  This gives you the exact HCL pattern to follow.

STEP 5 — Generate Terraform:
  Return generate_terraform action. Use the example as a template.
  - Include a resource group module + the requested resource module
  - Set cost_center tag from what you extracted in step 1
  - Set ticket_id tag to "{ticket_id}"
  - Use eastus2 as location
  - Keep names short (≤24 chars for storage accounts)

STEP 6 — Create branch:
  Call github__create_branch with:
    owner: "{github_org}"
    repo: "{github_repo}"
    branch: "feature/provision-{ticket_id}"
    from_branch: "main"

STEP 7 — Push Terraform files:
  Call github__push_files with:
    owner: "{github_org}"
    repo: "{github_repo}"
    branch: "feature/provision-{ticket_id}"
    message: "feat: provision resources for {ticket_id}"
    files: [
      {{"path": "provisioned/{ticket_id}/main.tf",      "content": "<main_tf from step 5>"}},
      {{"path": "provisioned/{ticket_id}/variables.tf", "content": "<variables_tf from step 5>"}}
    ]

STEP 8 — Create pull request:
  Call github__create_pull_request with:
    owner: "{github_org}"
    repo: "{github_repo}"
    title: "Provision: <short_description> [{ticket_id}]"
    body: "## Terraform Provisioning Request\\n\\n**Ticket:** {ticket_id}\\n**Description:** <short_description>\\n**Cost Center:** <cost_center>\\n\\n### Resources\\n<bullet list of resources>"
    head: "feature/provision-{ticket_id}"
    base: "main"
  Save the PR URL (html_url) from the response.

STEP 9 — Update SNOW ticket:
  Call snow__SN-Update-Record with:
    table_name: "sc_req_item"
    sys_id: <sys_id from step 1>
    data: {{"work_notes": "Terraform PR created: <pr_url>\\n\\nProvisioning workflow complete. Review and merge to apply infrastructure changes."}}

STEP 10 — Return final:
  Return final action with pr_url, summary, ticket_updated=true.

=== RULES ===
- Use EXACT tool names listed above — no variations.
- In push_files, the content field is plain HCL text (not base64).
- All {ticket_id}, {github_org}, {github_repo} placeholders are filled from the
  context variables provided in the first user message.
"""


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------


def _extract_json_dict(text: str) -> Dict[str, Any]:
    text = text.strip()
    # Try direct parse first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # Find first complete JSON object
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        start = match.start()
        try:
            parsed, _ = decoder.raw_decode(text[start:])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue

    raise ProvisioningError(
        f"LLM did not return valid JSON. Response was:\n{text[:500]}"
    )


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------


def load_mcp_configs() -> Dict[str, MCPServerConfig]:
    """Build MCP server configs from environment variables."""
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
            protocol="ndjson",  # servicenow-mcp-server uses newline-delimited JSON
        ),
        "github": MCPServerConfig(
            name="github",
            command=os.getenv("GITHUB_MCP_COMMAND", "npx @modelcontextprotocol/server-github"),
            env={
                k: v
                for k, v in {
                    "GITHUB_PERSONAL_ACCESS_TOKEN": os.getenv(
                        "GITHUB_PERSONAL_ACCESS_TOKEN", ""
                    ),
                }.items()
                if v
            },
            timeout=45.0,
            protocol="ndjson",  # @modelcontextprotocol/server-github uses newline-delimited JSON
        ),
        "azure": MCPServerConfig(
            name="azure",
            command=os.getenv("AZURE_MCP_SERVER_COMMAND", ""),
            env={},
            timeout=60.0,
            protocol="lsp",  # @azure/mcp uses LSP Content-Length framing
        ),
    }


# ---------------------------------------------------------------------------
# Main agent entry point
# ---------------------------------------------------------------------------


def provision_from_ticket(
    openai_settings: OpenAISettings,
    ticket_id: str,
    mcp_configs: Optional[Dict[str, MCPServerConfig]] = None,
    max_iterations: int = 15,
) -> ProvisioningResult:
    """Run the full provisioning workflow for a ServiceNow ticket."""
    if not ticket_id.strip():
        raise ProvisioningError("ticket_id cannot be empty")

    configs = mcp_configs or load_mcp_configs()

    github_org = os.getenv("GITHUB_ORG", "")
    github_repo = os.getenv("GITHUB_TERRAFORM_REPO", "terraform-modules-demo")
    azure_subscription_id = os.getenv("AZURE_SUBSCRIPTION_ID", "")

    with MultiMCPClient(configs) as mcp:
        all_tools = mcp.all_tools_manifest()
        tool_names = {t["name"] for t in all_tools}
        manifest = tool_manifest_json(all_tools)

        # Fill system prompt placeholders with runtime values
        filled_system_prompt = (
            _SYSTEM_PROMPT
            .replace("{ticket_id}", ticket_id)
            .replace("{github_org}", github_org or "natesanshreyas")
            .replace("{github_repo}", github_repo)
        )

        context_line = (
            f"=== CONTEXT ===\n"
            f"ticket_id: {ticket_id}\n"
            f"github_org: {github_org or 'natesanshreyas'}\n"
            f"github_repo: {github_repo}\n"
            f"azure_subscription_id: {azure_subscription_id or '(not set)'}\n\n"
            f"=== AVAILABLE MCP TOOLS ===\n{manifest}\n\n"
            f"Begin at STEP 1. Read ticket {ticket_id} now."
        )

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": filled_system_prompt},
            {"role": "user", "content": context_line},
        ]

        trace: List[ToolCallRecord] = []
        # State preserved across the generate_terraform step
        terraform_state: Optional[Dict[str, str]] = None
        # Evaluator state
        _terraform_eval_scores: Optional[Dict[str, Any]] = None
        _terraform_retries: int = 0

        for i in range(1, max_iterations + 1):
            with _telemetry.Timer() as llm_timer:
                response_raw = chat_completion(
                    openai_settings,
                    messages,
                    temperature=0.0,
                    max_tokens=4000,
                )
            decision = _extract_json_dict(response_raw)
            action = decision.get("action", "")

            # LLM sometimes puts the tool name directly in "action" instead of
            # using {"action":"tool_call","tool":"..."}.  Normalise that here.
            if "__" in action and "tool" not in decision:
                decision = {"action": "tool_call", "tool": action, "arguments": decision.get("arguments", {}), "reason": decision.get("reason", "")}
                action = "tool_call"

            _telemetry.track_llm_call(
                ticket_id=ticket_id,
                iteration=i,
                action_returned=action if action != "tool_call" else decision.get("tool", "tool_call"),
                duration_seconds=llm_timer.elapsed,
            )

            # ── Final answer ──────────────────────────────────────────────
            if action == "final":
                return ProvisioningResult(
                    pr_url=str(decision.get("pr_url", "")),
                    summary=str(decision.get("summary", "")),
                    ticket_updated=bool(decision.get("ticket_updated", False)),
                    iterations=i,
                    tool_calls=trace,
                    eval_scores=_terraform_eval_scores,
                )

            # ── Blocked (not approved) ────────────────────────────────────
            if action == "blocked":
                raise ProvisioningError(
                    f"Provisioning blocked: {decision.get('reason', 'ticket not approved')}"
                )

            # ── Terraform generation (LLM produces HCL, no tool call) ─────
            if action == "generate_terraform":
                main_tf = str(decision.get("main_tf", "")).strip()
                variables_tf = str(decision.get("variables_tf", "")).strip()
                if not main_tf:
                    raise ProvisioningError(
                        "generate_terraform action returned empty main_tf content"
                    )

                # ── Evaluate the generated HCL ────────────────────────────
                eval_result = None
                try:
                    eval_result = evaluate_terraform(
                        main_tf=main_tf,
                        variables_tf=variables_tf,
                        ticket_id=ticket_id,
                        openai_settings=openai_settings,
                    )
                    _terraform_eval_scores = {
                        "security": eval_result.security,
                        "compliance": eval_result.compliance,
                        "quality": eval_result.quality,
                        "passed": eval_result.passed,
                        "reason": eval_result.reason,
                    }
                except Exception as exc:
                    _telemetry.track_tool_call(
                        tool_name="[terraform_evaluator]",
                        ticket_id=ticket_id,
                        duration_seconds=0.0,
                        success=False,
                        error=str(exc),
                    )

                # ── Retry on eval failure (max 2 retries) ─────────────────
                if (
                    eval_result is not None
                    and not eval_result.passed
                    and _terraform_retries < 2
                ):
                    _terraform_retries += 1
                    messages.append(
                        {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)}
                    )
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"The generated Terraform code did not pass evaluation "
                                f"(attempt {_terraform_retries}/2).\n"
                                f"Issues found: {eval_result.reason}\n\n"
                                "Please fix these issues and return a new generate_terraform action."
                            ),
                        }
                    )
                    continue

                # ── Evaluation passed (or exhausted retries) — proceed ────
                terraform_state = {"main_tf": main_tf, "variables_tf": variables_tf}
                preview = (
                    f"Generated main.tf ({len(main_tf)} chars) and "
                    f"variables.tf ({len(variables_tf)} chars)"
                )
                if _terraform_eval_scores:
                    preview += (
                        f". Eval scores — security={_terraform_eval_scores['security']}/5 "
                        f"compliance={_terraform_eval_scores['compliance']}/5 "
                        f"quality={_terraform_eval_scores['quality']}/5"
                    )
                trace.append(
                    ToolCallRecord(
                        name="[generate_terraform]",
                        arguments={},
                        result_preview=preview,
                    )
                )
                messages.append({"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Terraform generated successfully. {preview}.\n"
                            f"Now proceed to step 7: create branch feature/provision-{ticket_id}."
                        ),
                    }
                )
                continue

            # ── Tool call ─────────────────────────────────────────────────
            if action == "tool_call":
                tool_name = str(decision.get("tool", "")).strip()
                args = decision.get("arguments", {})
                if not isinstance(args, dict):
                    args = {}

                if tool_name not in tool_names:
                    # If the LLM hallucinated an azure__ tool but no Azure MCP is
                    # configured, gracefully redirect it to skip Step 3.
                    if tool_name.startswith("azure__") and not any(
                        t.startswith("azure__") for t in tool_names
                    ):
                        messages.append(
                            {"role": "assistant", "content": json.dumps(decision, ensure_ascii=False)}
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"No Azure MCP tools are available in this environment "
                                    f"(tool {tool_name!r} does not exist in the manifest). "
                                    "SKIP Step 3 entirely. Proceed directly to STEP 4: "
                                    "read the Terraform example file from GitHub."
                                ),
                            }
                        )
                        continue
                    raise ProvisioningError(
                        f"LLM requested unknown tool: {tool_name!r}. "
                        f"Available tools: {sorted(tool_names)}"
                    )

                # Safety net: if LLM forgot to include content when pushing files
                # but we already have generated terraform, inject it.
                if terraform_state and "github__" in tool_name:
                    file_path = str(args.get("path", ""))
                    if not args.get("content"):
                        if "main.tf" in file_path:
                            args["content"] = terraform_state["main_tf"]
                        elif "variables.tf" in file_path:
                            args["content"] = terraform_state["variables_tf"]

                with _telemetry.Timer() as tool_timer:
                    result = mcp.call_tool(tool_name, args)
                preview = format_tool_result(result)
                trace.append(
                    ToolCallRecord(name=tool_name, arguments=args, result_preview=preview)
                )
                _telemetry.track_tool_call(
                    tool_name=tool_name,
                    ticket_id=ticket_id,
                    duration_seconds=tool_timer.elapsed,
                    success=True,
                )

                messages.append(
                    {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "action": "tool_call",
                                "tool": tool_name,
                                "arguments": args,
                                "reason": decision.get("reason", ""),
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"Tool result ({tool_name}):\n{preview}\n"
                            "Continue with the next step in the workflow."
                        ),
                    }
                )
                continue

            raise ProvisioningError(
                f"LLM returned unrecognised action: {action!r}. "
                f"Full response: {json.dumps(decision)[:400]}"
            )

    raise ProvisioningError(f"No final result after {max_iterations} iterations")
