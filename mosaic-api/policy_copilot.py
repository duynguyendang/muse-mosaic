"""Policy Copilot: natural language -> a *draft* policy, never a live one.

The architecture rule of the whole product is "proposers never publish".
The Copilot follows it one level up: a model (or, in mock mode, a small
deterministic parser) may only *propose* a change, expressed in a CLOSED
vocabulary (``PolicySpec``). Everything after that is deterministic code:

1. ``PolicySpec`` validates the proposal (known base profile, tier 1..3,
   identifier-shaped names). Anything outside the vocabulary is refused as
   ``unsupported`` with a reason; the Copilot never improvises a clause.
2. ``compile_spec`` rewrites the base profile's Horn text with fixed
   templates. The model never writes Datalog.
3. ``verify_with_engine`` hands the compiled text to ``mkit eval``. The
   manglekit analyzer rejects programs that are not stratified or not
   closed, so "compiles" is the engine's verdict, not ours. If no engine is
   reachable the draft is reported ``unverified``, never ``compiled``.

A draft is returned for a human to review. Nothing here writes policy
files, applies a profile or publishes a golden.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from config import Settings
from policy_profiles import load_profiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

IDENT = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
MAX_INSTRUCTION_CHARS = 500
MKIT_TIMEOUT_SECONDS = 20

SYSTEM_PROMPT = (
    "You translate a data-governance instruction into a policy change for a "
    "Datalog trust engine. You may ONLY express: (a) source_tiers: set a "
    "source system to trust tier 1 (lowest), 2 or 3 (highest); (b) "
    "multivalued_attributes: declare an attribute as repeatable so it is never "
    "uniqueness-constrained. If the instruction needs anything else (time "
    "windows, new rule logic, thresholds, deleting rules), set `unsupported` "
    "to a one-sentence reason and leave the other fields empty. Source and "
    "attribute names are free labels: use the name exactly as the instruction "
    "gives it (e.g. 'marketplace-like', 'order_id'). 'Trust X least' means tier 1, "
    "'trust X most' means tier 3. Never invent names the instruction does not mention."
)

SPEC_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "base_profile": {
            "type": "string",
            "enum": ["strict_bcbs239", "retail_balanced", "analytical_mesh"],
        },
        # Strict mode forbids free-form maps, so tiers are a list of pairs.
        "source_tiers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source": {"type": "string"},
                    "tier": {"type": "integer", "enum": [1, 2, 3]},
                },
                "required": ["source", "tier"],
            },
        },
        "multivalued_attributes": {"type": "array", "items": {"type": "string"}},
        "unsupported": {"type": ["string", "null"]},
    },
    "required": ["base_profile", "source_tiers", "multivalued_attributes", "unsupported"],
}


class PolicySpec(BaseModel):
    """The only things a draft may change. Closed on purpose."""

    model_config = ConfigDict(extra="forbid")

    base_profile: Literal["strict_bcbs239", "retail_balanced", "analytical_mesh"]
    source_tiers: dict[str, int] = Field(default_factory=dict)
    multivalued_attributes: list[str] = Field(default_factory=list)
    unsupported: str | None = None

    def problems(self) -> list[str]:
        out: list[str] = []
        for source, tier in self.source_tiers.items():
            if not IDENT.match(source):
                out.append(f"source name {source!r} is not an identifier")
            if tier not in (1, 2, 3):
                out.append(f"tier for {source!r} must be 1, 2 or 3 (got {tier})")
        for attr in self.multivalued_attributes:
            if not IDENT.match(attr.replace("-", "_")):
                out.append(f"attribute name {attr!r} is not an identifier")
        return out

    @property
    def is_empty(self) -> bool:
        return not self.source_tiers and not self.multivalued_attributes


# --------------------------------------------------------------------------
# Step 1: propose (mock parser or live model)
# --------------------------------------------------------------------------

_TIER_WORDS = {"highest": 3, "top": 3, "most trusted": 3, "lowest": 1, "least trusted": 1}
_SRC = r"([a-z][a-z0-9_-]*)"


def _norm_attr(name: str) -> str:
    return name.strip().lower().replace("-", "_")


def mock_propose(instruction: str, base_profile: str) -> PolicySpec:
    """Deterministic parser for a tiny fixed grammar. Not an LLM.

    Recognised:  "treat crm as tier 3" / "set orders to tier 2"
                 "trust sap-like most" / "trust crm least"
                 "order_id is multivalued" / "allow multiple order_id"
    Anything else is refused rather than guessed.
    """
    text = instruction.strip().lower()
    tiers: dict[str, int] = {}
    multi: list[str] = []

    for m in re.finditer(rf"(?:treat|set|make|rank)\s+{_SRC}\s+(?:as|to)?\s*tier\s*([123])", text):
        tiers[m.group(1)] = int(m.group(2))
    for m in re.finditer(rf"trust\s+{_SRC}\s+(most|highest|least|lowest)", text):
        tiers[m.group(1)] = 3 if m.group(2) in ("most", "highest") else 1
    for m in re.finditer(rf"{_SRC}\s+(?:is|are)\s+multi-?valued", text):
        multi.append(_norm_attr(m.group(1)))
    for m in re.finditer(rf"allow\s+multiple\s+{_SRC}", text):
        multi.append(_norm_attr(m.group(1)))

    if not tiers and not multi:
        return PolicySpec(
            base_profile=base_profile,  # type: ignore[arg-type]
            unsupported=(
                "Not understood by the offline parser. Supported: 'treat <source> as tier "
                "<1-3>', 'trust <source> most|least', '<attribute> is multivalued'. "
                "Time windows and new rule logic need a live model and are still refused."
            ),
        )
    return PolicySpec(
        base_profile=base_profile,  # type: ignore[arg-type]
        source_tiers=tiers,
        multivalued_attributes=sorted(set(multi)),
    )


async def live_propose(settings: Settings, instruction: str, base_profile: str) -> PolicySpec:
    from meta_client import chat_json

    parsed, _usage = await chat_json(
        settings,
        model=settings.muse_model_id,
        system=SYSTEM_PROMPT,
        user=f"Base profile: {base_profile}\nInstruction: {instruction}",
        schema_name="mosaic_policy_spec",
        schema=SPEC_JSON_SCHEMA,
        reasoning_effort="low",
        prompt_cache_key="mosaic-policy-copilot-v1",
    )
    parsed["base_profile"] = base_profile  # the caller's choice, not the model's
    pairs = parsed.get("source_tiers") or []
    if isinstance(pairs, list):
        parsed["source_tiers"] = {
            str(p["source"]).strip().lower(): p["tier"] for p in pairs if isinstance(p, dict)
        }
    return PolicySpec.model_validate(parsed)


# --------------------------------------------------------------------------
# Step 2: compile with fixed templates
# --------------------------------------------------------------------------

_TIER_LINE = re.compile(r'^source_tier\("([^"]+)",\s*(\d)\)\.\s*$')


def compile_spec(horn: str, spec: PolicySpec) -> tuple[str, list[str]]:
    """Apply ``spec`` to Horn text. Returns (new_text, refusal reasons)."""
    reasons: list[str] = []
    lines = horn.splitlines()
    tier_idx = [i for i, ln in enumerate(lines) if _TIER_LINE.match(ln.strip())]

    if spec.source_tiers and not tier_idx:
        reasons.append(
            f"Profile {spec.base_profile!r} has no source hierarchy (source_tier facts); "
            "tier overrides are not meaningful there."
        )
    else:
        for source, tier in spec.source_tiers.items():
            replaced = False
            for i in tier_idx:
                m = _TIER_LINE.match(lines[i].strip())
                if m and m.group(1) == source:
                    lines[i] = f'source_tier("{source}", {tier}).'
                    replaced = True
            if not replaced:
                lines.insert(tier_idx[-1] + 1, f'source_tier("{source}", {tier}).')
                tier_idx.append(tier_idx[-1] + 1)

    existing_multi = {
        m.group(1)
        for ln in lines
        if (m := re.match(r'^multivalued\("([^"]+)"\)\.\s*$', ln.strip()))
    }
    anchor = next(
        (i for i, ln in enumerate(lines) if re.match(r'^\s*multivalued\("', ln)),
        None,
    )
    for attr in spec.multivalued_attributes:
        if attr in existing_multi:
            continue
        new_line = f'multivalued("{attr}").'
        if anchor is None:
            lines.append(new_line)
        else:
            lines.insert(anchor + 1, new_line)

    return "\n".join(lines) + ("\n" if horn.endswith("\n") else ""), reasons


def line_diff(before: str, after: str) -> list[str]:
    return [
        ln
        for ln in difflib.unified_diff(
            before.splitlines(), after.splitlines(), "base", "draft", lineterm="", n=0
        )
        if ln[:1] in "+-" and not ln.startswith(("+++", "---"))
    ]


# --------------------------------------------------------------------------
# Step 3: ask the engine
# --------------------------------------------------------------------------

_PROBE_INPUT = {
    "facts": [],
    "attributes": [],
    "links": [],
    "anchor_fact": "",
    "taxonomy": {"version": "probe", "terms": []},
    "now_min": 0,
}


def find_mkit() -> str | None:
    env = os.environ.get("MANGLEKIT_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("mkit") or (
        "/tmp/mkitbuild/mkit" if Path("/tmp/mkitbuild/mkit").exists() else None
    )


def verify_with_engine(horn: str) -> dict[str, Any]:
    """Run the draft through ``mkit eval``. The analyzer is the judge."""
    binary = find_mkit()
    if binary is None:
        return {"engine_available": False, "compiles": None, "error": "mkit binary not found"}
    with tempfile.TemporaryDirectory(prefix="mosaic-copilot-") as tmp:
        policy = Path(tmp) / "draft.mg"
        data = Path(tmp) / "input.json"
        policy.write_text(horn, encoding="utf-8")
        data.write_text(json.dumps(_PROBE_INPUT), encoding="utf-8")
        try:
            proc = subprocess.run(
                [binary, "eval", "-p", str(policy), "-d", str(data), "-q", "outcome(A, V, X, R)"],
                capture_output=True,
                text=True,
                timeout=MKIT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {"engine_available": True, "compiles": False, "error": "engine timed out"}
    if proc.returncode != 0:
        return {
            "engine_available": True,
            "compiles": False,
            "error": (proc.stderr or proc.stdout).strip()[:400],
        }
    return {"engine_available": True, "compiles": True, "error": None}


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


async def draft(settings: Settings, instruction: str, base_profile: str) -> dict[str, Any]:
    """Never raises for model/engine trouble: reports a refusal status."""
    instruction = instruction.strip()
    if not instruction or len(instruction) > MAX_INSTRUCTION_CHARS:
        return _refused("invalid", f"Instruction must be 1-{MAX_INSTRUCTION_CHARS} characters.")
    profile = load_profiles().get(base_profile)
    if profile is None:
        return _refused("invalid", f"Unknown base profile {base_profile!r}.")

    proposer = "mock" if settings.use_mock_muse else "live"
    try:
        spec = (
            mock_propose(instruction, base_profile)
            if settings.use_mock_muse
            else await live_propose(settings, instruction, base_profile)
        )
    except (ValidationError, ValueError) as exc:
        return _refused("invalid", f"Model output left the closed vocabulary: {exc}", proposer)
    except Exception as exc:  # noqa: BLE001 - model transport failure must fail closed
        return _refused("unavailable", f"Policy proposer unavailable: {exc}", proposer)

    if spec.unsupported:
        return _refused("unsupported", spec.unsupported, proposer, spec)
    problems = spec.problems()
    if problems or spec.is_empty:
        return _refused("invalid", "; ".join(problems) or "Empty change.", proposer, spec)

    entity = profile.default_entity_type
    base_horn = profile.policies[entity].horn_clauses
    new_horn, reasons = compile_spec(base_horn, spec)
    if reasons:
        return _refused("unsupported", " ".join(reasons), proposer, spec)
    diff = line_diff(base_horn, new_horn)
    if not diff:
        return _refused(
            "no_change",
            "The base profile already has exactly these settings; nothing to draft.",
            proposer,
            spec,
        )
    checks = verify_with_engine(new_horn)
    if checks["compiles"] is True:
        status = "compiled"
    elif checks["compiles"] is False:
        status = "rejected_by_engine"
    else:
        status = "unverified"
    return {
        "status": status,
        "proposer": proposer,
        "spec": spec.model_dump(),
        "entity_type": entity,
        "diff": diff,
        "checks": checks,
        "applied": False,
        "note": "Draft only. Nothing was staged, applied or published.",
    }


def _refused(
    status: str, message: str, proposer: str | None = None, spec: PolicySpec | None = None
) -> dict[str, Any]:
    return {
        "status": status,
        "proposer": proposer,
        "message": message,
        "spec": spec.model_dump() if spec else None,
        "diff": [],
        "checks": None,
        "applied": False,
    }
