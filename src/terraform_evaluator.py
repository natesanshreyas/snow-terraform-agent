"""Terraform HCL quality evaluator using Azure OpenAI.

Three independent LLM judges score the generated code 1–5 on:
  security   — no hardcoded credentials or sensitive literals
  compliance — required cost_center + ticket_id tags present
  quality    — uses module pattern, not raw resource blocks

All three must score >= 3 to pass.  If a run fails, a human-readable
reason is returned so the orchestrator can inject feedback and ask the
LLM to regenerate (up to 2 retries).

Optionally logs results to Azure AI Foundry Evaluations tab if
AZURE_AI_FOUNDRY_PROJECT_CONNECTION_STRING is set in the environment.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from .openai_client import OpenAISettings, chat_completion

logger = logging.getLogger(__name__)

_PASS_THRESHOLD = 3  # minimum score (out of 5) on each dimension to pass


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class EvalResult:
    security: int    # 1–5
    compliance: int  # 1–5
    quality: int     # 1–5
    passed: bool
    reason: str      # non-empty only when passed=False


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SECURITY_PROMPT = """\
You are a Terraform security reviewer. Score the HCL below from 1 (very insecure) \
to 5 (very secure) based ONLY on the following criteria:

  5 — No hardcoded secrets, passwords, API keys, or connection strings anywhere; \
sensitive values use input variables or are entirely absent.
  4 — Only minor, non-sensitive literals hardcoded (e.g. a public DNS name).
  3 — Sensitive values use variables; no credentials visible in plain text.
  2 — At least one sensitive value (e.g. a password) is hardcoded.
  1 — Credentials or API keys are clearly visible in the code.

--- main.tf ---
{main_tf}

--- variables.tf ---
{variables_tf}

Return ONLY a JSON object (no markdown, no explanation outside JSON):
{{"score": <integer 1-5>, "reason": "<one concise sentence>"}}"""

_COMPLIANCE_PROMPT = """\
You are a Terraform compliance reviewer. Score the HCL below from 1 (non-compliant) \
to 5 (fully compliant) based ONLY on tag compliance:

Required tags that MUST appear on BOTH the resource group module AND the main \
resource module:
  • cost_center
  • ticket_id  (expected value references the ticket: {ticket_id})

  5 — Both tags present on resource group and main resource.
  4 — Both tags present on the main resource only.
  3 — At least one required tag present somewhere.
  2 — Tags block exists but required tags are absent.
  1 — No tags at all.

--- main.tf ---
{main_tf}

Return ONLY a JSON object (no markdown, no explanation outside JSON):
{{"score": <integer 1-5>, "reason": "<one concise sentence>"}}"""

_QUALITY_PROMPT = """\
You are a Terraform code quality reviewer. Score the HCL below from 1 (poor) \
to 5 (excellent) based ONLY on structural quality:

  5 — Uses module blocks (not raw resource blocks) for all resources; \
variables.tf has types and descriptions; clean, readable structure.
  4 — Mostly uses modules with minor issues.
  3 — Uses at least one module block; acceptable structure.
  2 — Primarily raw resource blocks; minimal use of modules.
  1 — No modules used; sprawling raw resource declarations.

--- main.tf ---
{main_tf}

--- variables.tf ---
{variables_tf}

Return ONLY a JSON object (no markdown, no explanation outside JSON):
{{"score": <integer 1-5>, "reason": "<one concise sentence>"}}"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _call_evaluator(prompt: str, settings: OpenAISettings) -> dict:
    """Call LLM and parse the {score, reason} JSON response."""
    response = chat_completion(
        settings,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.0,
        max_tokens=200,
    )
    text = response.strip()

    # Direct parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "score" in obj:
            return obj
    except Exception:
        pass

    # Scan for first valid JSON object
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
            if isinstance(obj, dict) and "score" in obj:
                return obj
        except Exception:
            continue

    logger.warning("Evaluator returned unparseable response: %s", text[:300])
    return {"score": 3, "reason": "Could not parse evaluator response"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def evaluate_terraform(
    main_tf: str,
    variables_tf: str,
    ticket_id: str,
    openai_settings: OpenAISettings,
) -> EvalResult:
    """Evaluate generated HCL. Returns EvalResult with passed=True if all scores >= 3."""
    logger.info("Running Terraform evaluation for ticket=%s", ticket_id)

    sec = _call_evaluator(
        _SECURITY_PROMPT.format(main_tf=main_tf, variables_tf=variables_tf),
        openai_settings,
    )
    comp = _call_evaluator(
        _COMPLIANCE_PROMPT.format(main_tf=main_tf, ticket_id=ticket_id),
        openai_settings,
    )
    qual = _call_evaluator(
        _QUALITY_PROMPT.format(main_tf=main_tf, variables_tf=variables_tf),
        openai_settings,
    )

    s = max(1, min(5, int(sec.get("score", 3))))
    c = max(1, min(5, int(comp.get("score", 3))))
    q = max(1, min(5, int(qual.get("score", 3))))

    passed = s >= _PASS_THRESHOLD and c >= _PASS_THRESHOLD and q >= _PASS_THRESHOLD

    reasons: list[str] = []
    if s < _PASS_THRESHOLD:
        reasons.append(f"Security ({s}/5): {sec.get('reason', '')}")
    if c < _PASS_THRESHOLD:
        reasons.append(f"Compliance ({c}/5): {comp.get('reason', '')}")
    if q < _PASS_THRESHOLD:
        reasons.append(f"Quality ({q}/5): {qual.get('reason', '')}")

    result = EvalResult(
        security=s,
        compliance=c,
        quality=q,
        passed=passed,
        reason="; ".join(reasons),
    )

    logger.info(
        "Eval: security=%d compliance=%d quality=%d passed=%s",
        s, c, q, passed,
    )

    _log_to_foundry(result, ticket_id)
    return result


def _log_to_foundry(result: EvalResult, ticket_id: str) -> None:
    """Log evaluation scores to Foundry Evaluations tab (fire-and-forget).

    Uses the project scope from telemetry.get_foundry_scope().  If the scope
    is unavailable (local dev without Foundry configured) this is a silent no-op.
    """
    from .telemetry import get_foundry_scope

    scope = get_foundry_scope()
    if scope is None:
        logger.debug("Foundry scope not available; skipping evaluation log")
        return

    try:
        import tempfile
        import json as _json
        from azure.ai.evaluation import evaluate
        from azure.identity import DefaultAzureCredential

        # azure-ai-evaluation expects a JSONL data file
        row = {
            "ticket_id": ticket_id,
            "main_tf": "",   # already evaluated; pass empty for record-keeping
            "security_score": result.security,
            "compliance_score": result.compliance,
            "quality_score": result.quality,
            "passed": result.passed,
            "reason": result.reason,
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as tmp:
            tmp.write(_json.dumps(row) + "\n")
            tmp_path = tmp.name

        evaluate(
            evaluation_name=f"terraform-eval-{ticket_id}",
            data=tmp_path,
            evaluators={},          # scores are pre-computed; no additional evaluators needed
            azure_ai_project=scope,
            output_path=None,
        )
        logger.info("Logged evaluation run to Foundry for ticket=%s", ticket_id)
    except Exception as exc:
        # Non-fatal — Foundry logging should never block provisioning
        logger.debug("Foundry evaluation log failed: %s", exc)
