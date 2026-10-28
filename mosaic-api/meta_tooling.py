"""Conditional Meta tooling: Wonder adjudication + Prompt Guard screening.

CSD 2.9 rule: a Meta tool counts only if the demo can show its call, its
output, and its limit. Both tools below are CONDITIONAL:

- Wonder adjudicates escalations only, never proposes/publishes/overrides.
  Used only if WONDER_MODEL_ID is pinned; otherwise a labelled stub that
  never claims a real call.
- Prompt Guard screens untrusted fields only. No verified Prompt Guard API
  is assumed in this document, so screening is Pydantic validation + claim
  logging (LocalHeuristicVerifier), labelled as such. A verified connector
  implements PromptGuardVerifier and replaces it without touching callers.

No Meta tool is ever called in tests: mock paths return labelled stubs.
"""

from __future__ import annotations

from typing import Any

from config import Settings
from ingest import is_injection_bearing
from meta_client import MetaCallError, chat_json

WONDER_SYSTEM = """You adjudicate an escalated merge conflict. You do NOT publish,
pick a winner, create taxonomy terms, or make the steward decision. Output JSON
only: {"note": str, "stronger_side": str, "missing_evidence": str,
"check_first": str}. Note which side has stronger evidence, what is still
missing, and what a steward should check first."""

WONDER_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "stronger_side": {"type": "string"},
        "missing_evidence": {"type": "string"},
        "check_first": {"type": "string"},
    },
    "required": ["note", "stronger_side", "missing_evidence", "check_first"],
    "additionalProperties": False,
}


class PromptGuardVerifier:
    """Interface for a verified Prompt Guard connector (not assumed)."""

    def screen(self, field_name: str, value: str) -> dict[str, Any]:
        raise NotImplementedError


class LocalHeuristicVerifier(PromptGuardVerifier):
    """Fallback screening: Pydantic-shaped claim logging, labelled as such.

    Flags instruction-override markers and suspicious encodings. The flag is
    a claim attached to the field; a suspicious field is still shown, and a
    blocked field never becomes a trusted fact (the engine still decides).
    """

    def screen(self, field_name: str, value: str) -> dict[str, Any]:
        lowered = value.lower()
        suspicious_markers = [
            "ignore previous instructions",
            "system override",
            "override:",
            "delete the record",
            "delete the old",
        ]
        hit = next((marker for marker in suspicious_markers if marker in lowered), None)
        if hit:
            return {
                "field": field_name,
                "flag": "suspicious",
                "reason": f"instruction-override marker: {hit!r}",
                "source": "local-heuristic (Prompt Guard not verified)",
            }
        if any(ord(char) < 32 and char not in ("\n", "\t") for char in value):
            return {
                "field": field_name,
                "flag": "suspicious",
                "reason": "control characters in untrusted field",
                "source": "local-heuristic (Prompt Guard not verified)",
            }
        return {
            "field": field_name,
            "flag": "clean",
            "reason": "no markers found",
            "source": "local-heuristic (Prompt Guard not verified)",
        }


def screen_untrusted_field(field_name: str, value: str) -> dict[str, Any]:
    """Screen one untrusted field (ticket/device/free-text)."""
    return LocalHeuristicVerifier().screen(field_name, value)


def is_prompt_guard_verified() -> bool:
    """No verified Prompt Guard connector exists in v1; always fallback."""
    return False


def wonder_status() -> dict[str, Any]:
    """Honest inventory row for the README/video claim check."""
    return {
        "tool": "Muse Wonder",
        "status": "stubbed",
        "detail": "Wonder adjudication is conditional on a verified model id; "
        "not verified in this run, never simulated as real.",
    }


async def wonder_adjudicate(
    settings: Settings,
    *,
    attribute: str,
    entity_type: str,
    entity_id: str,
    evidence: dict[str, Any],
    wonder_model_id: str | None = None,
) -> dict[str, Any]:
    """Adjudicate ONE escalation for the steward. Never publishes.

    Requires an explicit wonder_model_id (conditional per CSD A-19). Without
    it, returns a labelled stub. Called only on escalations, never on every
    build — cost control per CSD 2.9.
    """
    if not wonder_model_id:
        return {
            "status": "not_available",
            "attribute": attribute,
            "note": "Wonder: not available in this run (no verified model id).",
        }
    if settings.use_mock_muse:
        return {
            "status": "not_available",
            "attribute": attribute,
            "note": "Wonder: not available in mock mode.",
        }
    import json as jsonlib

    try:
        parsed, usage = await chat_json(
            settings,
            model=wonder_model_id,
            system=WONDER_SYSTEM,
            user=(
                f"Escalated attribute {attribute!r} for {entity_type}/{entity_id}.\n"
                f"Evidence:\n{jsonlib.dumps(evidence, ensure_ascii=False, indent=1)}"
            ),
            schema_name="wonder_adjudication",
            schema=WONDER_SCHEMA,
            reasoning_effort="high",
            prompt_cache_key="mosaic-wonder-v1",
        )
    except MetaCallError as exc:
        return {
            "status": "failed",
            "attribute": attribute,
            "note": f"Wonder adjudication failed ({exc.kind}); steward decides unaided.",
        }
    parsed["status"] = "adjudicated"
    parsed["attribute"] = attribute
    parsed["usage"] = usage
    parsed["model"] = wonder_model_id
    return parsed


def is_injection_bearing_text(value: str) -> bool:
    """Alias kept near the screening boundary (see ingest.is_injection_bearing)."""
    return is_injection_bearing(value)
