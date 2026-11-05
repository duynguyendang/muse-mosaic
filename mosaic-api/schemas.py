"""Pydantic contracts for the mosaic-api <-> trust-engine boundary.

The fact JSON contract is the trust boundary (CSD 2.4 / 12.4). Candidate facts
without provenance are rejected here, before the trust-engine sees them. The
embedding must never be a derivation input, so it is not a field on
CandidateFact and extra fields are forbidden: an ``embedding`` field sent to
the derive contract is a validation error, not an ignored field.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENTITY_TYPE_PATTERN = r"^[a-z][a-z0-9_]*$"
ATTRIBUTE_PATTERN = r"^[a-z][a-z0-9_]*$"


class StrictModel(BaseModel):
    """Base model used at every service boundary."""

    model_config = ConfigDict(extra="forbid")


def _require_timezone(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone offset")
    return value


class CandidateFact(StrictModel):
    """A provenance-bearing fact. This is the only derivation input.

    NOTE: embedding is deliberately absent. Embeddings live only in the SQLite
    vector column for similar-search recall and must never enter derivation.
    """

    fact_id: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    attribute: str = Field(pattern=ATTRIBUTE_PATTERN)
    value: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    authority_rank: int = Field(ge=1)
    event_time: datetime
    verified: bool
    ttl_seconds: int = Field(gt=0)

    @field_validator("event_time")
    @classmethod
    def _event_time_has_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "event_time")


class Fragment(StrictModel):
    """A source fragment accepted by POST /resolve, before fact ids are assigned."""

    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    attribute: str = Field(pattern=ATTRIBUTE_PATTERN)
    value: str = Field(min_length=1)
    authority_rank: int = Field(ge=1)
    event_time: datetime
    verified: bool
    ttl_seconds: int = Field(gt=0)

    @field_validator("event_time")
    @classmethod
    def _event_time_has_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "event_time")


class GoldenFact(StrictModel):
    """A derived value. Every field needed for proof is mandatory."""

    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    attribute: str = Field(pattern=ATTRIBUTE_PATTERN)
    value: str = Field(min_length=1)
    winner_fact_id: str = Field(min_length=1)
    loser_fact_ids: list[str] = Field(default_factory=list)
    rule_fired: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    taxonomy_version: str | None = None
    lineage: str = Field(min_length=1)

    @model_validator(mode="after")
    def winner_is_not_a_loser(self) -> GoldenFact:
        if self.winner_fact_id in self.loser_fact_ids:
            raise ValueError("winner_fact_id cannot also appear in loser_fact_ids")
        return self


class VerifiedLink(StrictModel):
    """An engine-verified link between two facts (link proof L1)."""

    from_fact_id: str = Field(min_length=1)
    to_fact_id: str = Field(min_length=1)
    proof: str = Field(min_length=1)
    rule_fired: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    taxonomy_version: str | None = None


class GoldenRecord(StrictModel):
    """Latest published golden record as returned by GET /golden."""

    golden_id: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    attributes: dict[str, Any]
    decision_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    taxonomy_version: str | None = None
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class ConflictFact(StrictModel):
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    attribute: str = Field(pattern=ATTRIBUTE_PATTERN)
    reason_code: str = Field(min_length=1)
    rule_fired: str = Field(min_length=1)
    fact_ids: list[str] = Field(default_factory=list)
    policy_version: str = Field(min_length=1)
    taxonomy_version: str | None = None


class StewardItem(StrictModel):
    steward_id: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    attribute: str = Field(pattern=ATTRIBUTE_PATTERN)
    owner: str = Field(min_length=1)
    deputy: str | None = None
    deadline: datetime
    fallback: str = Field(min_length=1)
    status: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    suggested_action: str | None = None
    suggested_action: str | None = None

    @field_validator("deadline")
    @classmethod
    def _deadline_has_timezone(cls, value: datetime) -> datetime:
        return _require_timezone(value, "deadline")


class ResolveRequest(StrictModel):
    work_id: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    fragments: list[Fragment] = Field(min_length=1)


class ResolveResponse(StrictModel):
    decision_id: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    verdict: Literal["published", "escalated", "denied"]
    golden: GoldenRecord | None = None
    conflicts: list[ConflictFact] = Field(default_factory=list)
    steward_queue: list[StewardItem] = Field(default_factory=list)
    policy_version: str = Field(min_length=1)
    taxonomy_version: str | None = None


class DeriveRequest(StrictModel):
    decision_id: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    payload_hash: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    candidate_facts: list[CandidateFact] = Field(min_length=1)
    links: list[ProposedLink] = Field(default_factory=list)

    @model_validator(mode="after")
    def facts_match_envelope(self) -> DeriveRequest:
        fact_ids: set[str] = set()
        for fact in self.candidate_facts:
            if fact.entity_type != self.entity_type:
                raise ValueError(f"fact {fact.fact_id!r} entity_type does not match the envelope")
            if fact.entity_id != self.entity_id:
                raise ValueError(f"fact {fact.fact_id!r} entity_id does not match the envelope")
            if fact.fact_id in fact_ids:
                raise ValueError(f"duplicate fact_id {fact.fact_id!r}")
            fact_ids.add(fact.fact_id)
        return self


class DeriveResponse(StrictModel):
    """Derivation output. Empty golden_facts means nothing may be published.

    verdict and rule_fired are the engine's own summary of this derivation;
    mosaic-api may only downgrade (publish -> escalate), never upgrade.
    """

    decision_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    verdict: Literal["published", "escalated", "denied"] = "denied"
    rule_fired: str | None = None
    golden_facts: list[GoldenFact] = Field(default_factory=list)
    conflicts: list[ConflictFact] = Field(default_factory=list)
    steward_queue: list[StewardItem] = Field(default_factory=list)
    verified_links: list[VerifiedLink] = Field(default_factory=list)


class ExplainResponse(StrictModel):
    """Audit proof for one decision. Vector scores are never a rule here."""

    decision_id: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    verdict: str = Field(min_length=1)
    rule_fired: str | None = None
    winner_fact_id: str | None = None
    loser_fact_ids: list[str] = Field(default_factory=list)
    payload_hash: str | None = None
    created_at: str | None = None
    authority_ranks: dict[str, int] = Field(default_factory=dict)
    event_times: dict[str, str] = Field(default_factory=dict)
    input_fact_ids: list[str] = Field(default_factory=list)
    input_facts: list[dict[str, Any]] = Field(default_factory=list)
    policy_version: str = Field(min_length=1)
    taxonomy_version: str | None = None
    proposal: Proposal | None = None
    conflicts: list[ConflictFact] = Field(default_factory=list)
    steward_queue: list[StewardItem] = Field(default_factory=list)
    derivation_tree: list[str] = Field(default_factory=list)
    subagent_trace: list[dict[str, Any]] = Field(default_factory=list)
    wonder_note: dict[str, Any] | None = None
    prompt_guard_flag: dict[str, Any] | None = None


class HealthResponse(StrictModel):
    mosaic_api: Literal["ok"]
    trust_engine: Literal["ok", "down"]
    splink: Literal["ok", "unavailable"] | None = None
    splink_version: str | None = None
    duckdb_version: str | None = None
    policy_version_user: str | None = None
    policy_version_product: str | None = None
    # Which model drives the proposer agents. "mock" means deterministic local
    # agents; "live" means the model id below, via the Meta Model API.
    proposer_mode: Literal["mock", "live"] | None = None
    proposer_model: str | None = None


class ProposalAttribute(StrictModel):
    """One attribute of a Muse merge proposal (never published directly)."""

    proposed_value: str | None = None
    confidence: Literal["high", "medium", "low", "abstain"]
    explanation: str = Field(min_length=1)
    evidence_fact_ids: list[str] = Field(default_factory=list)
    injection_detected: bool = False


class LinkClaim(StrictModel):
    """A proposed link between two facts. A claim, never a verified link."""

    from_fact_id: str = Field(min_length=1)
    to_fact_id: str = Field(min_length=1)
    proof: str = Field(min_length=1)
    proposer: str = Field(min_length=1)


class ProposedLink(LinkClaim):
    """A LinkClaim with the proposer recorded (Splink / Muse / fuzzy / vector)."""


LINK_PROOFS = {
    "exact_email",
    "exact_phone",
    "exact_order",
    "exact_address",
    "fuzzy_name_address",
    "vector_only",
    "same_email_different_phone",
    "same_phone_different_email",
    "same_order_two_users",
    "same_fragment",
    # Muse semantic claims; the trust-engine verifies them (policy rules S1/S2).
    "semantic_phone",
    "semantic_email",
}

SOLE_LINK_PROOFS = {"exact_email", "exact_phone", "exact_order"}


class SubAgentStep(StrictModel):
    """One sub-agent step in the builder trace (claim only, never a decision)."""

    agent: str = Field(min_length=1)
    model: str | None = None
    real_call: bool = False
    output_summary: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)


class TaxonomyProposal(StrictModel):
    """A proposed new term or mapping. Steward-only Accept, never auto-applied."""

    kind: Literal["create_term", "map_synonym"]
    raw_value: str = Field(min_length=1)
    target_term: str | None = None
    suggested_action: str = Field(min_length=1)


class Proposal(StrictModel):
    mode: str = Field(min_length=1)
    model: str | None = None
    attributes: dict[str, ProposalAttribute] = Field(default_factory=dict)
    note: str = ""
    subagent_trace: list[SubAgentStep] = Field(default_factory=list)
    links: list[ProposedLink] = Field(default_factory=list)
    new_terms: list[TaxonomyProposal] = Field(default_factory=list)
    critiques: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)


class StewardApproveRequest(StrictModel):
    """Approve with an exact payload. The value is bound to this approval."""

    value: str = Field(min_length=1)


class StewardSplitRequest(StrictModel):
    """Restore the golden snapshot of a prior version."""

    restore_version: int = Field(ge=1)


class SimilarHit(StrictModel):
    fact_id: str = Field(min_length=1)
    attribute: str = Field(min_length=1)
    value: str = Field(min_length=1)
    source: str = Field(min_length=1)
    authority_rank: int = Field(ge=1)
    verified: bool
    event_time: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    vector_score: float | None = None
    vector_note: str = ""


class SimilarResponse(StrictModel):
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    results: list[SimilarHit] = Field(default_factory=list)
    vector_recall: str = Field(min_length=1)


class FragmentsResponse(StrictModel):
    synthetic: bool
    notice: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    fragments: list[Fragment] = Field(default_factory=list)
    tickets: list[dict[str, Any]] = Field(default_factory=list)


class EntityRef(StrictModel):
    """One definable synthetic entity (from data/mock fixtures)."""

    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    fragments: int = Field(ge=0)


class EntitiesResponse(StrictModel):
    synthetic: bool
    notice: str = Field(min_length=1)
    entities: list[EntityRef] = Field(default_factory=list)


class SplinkSettings(StrictModel):
    """Per-type Splink candidate settings. Demo parameters, versioned in policy."""

    threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    blocking: list[str] = Field(default_factory=list)


class Policy(StrictModel):
    """Externalized policy file shape (CSD 12.7, v2.3 rule tiers).

    ``thresholds`` carries demo parameters (low_confidence note, borderline
    band, Splink threshold): reviewed once before recording, never calibrated
    in code. ``splink`` carries per-type candidate settings (blocking +
    threshold); Splink proposes only, never decides.
    """

    policy_version: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    allowed_attributes: list[str] = Field(min_length=1)
    golden_attributes: list[str] = Field(default_factory=list)
    authority_matrix: dict[str, dict[str, int]]
    critical_attributes: list[str] = Field(default_factory=list)
    rules: list[str] = Field(min_length=1)
    thresholds: dict[str, Any] = Field(default_factory=dict)
    ttls_seconds: dict[str, int] = Field(default_factory=dict)
    splink: SplinkSettings = Field(default_factory=SplinkSettings)

    @model_validator(mode="after")
    def policy_is_coherent(self) -> Policy:
        if len(set(self.allowed_attributes)) != len(self.allowed_attributes):
            raise ValueError("allowed_attributes contains duplicates")
        allowed = set(self.allowed_attributes)
        for attribute, sources in self.authority_matrix.items():
            if attribute not in allowed:
                raise ValueError(f"authority_matrix attribute {attribute!r} is not allowed")
            for source, rank in sources.items():
                if not source:
                    raise ValueError("authority_matrix source names cannot be empty")
                if rank < 1:
                    raise ValueError(f"authority rank for {attribute!r}/{source!r} must be >= 1")
        for attribute in self.critical_attributes:
            if attribute not in allowed:
                raise ValueError(f"critical attribute {attribute!r} is not in allowed_attributes")
        if not set(self.golden_attributes or list(allowed)) <= allowed:
            raise ValueError("golden_attributes must be a subset of allowed_attributes")
        for window, seconds in self.ttls_seconds.items():
            if seconds <= 0:
                raise ValueError(f"ttl_seconds for {window!r} must be > 0")
        return self


# ---------------------------------------------------------------------------
# v2.3 additions: graph, taxonomy, build/upload, downstream
# (LinkClaim/ProposedLink/SubAgentStep/TaxonomyProposal live above Proposal
# so the proposal contract can embed the builder trace.)
# ---------------------------------------------------------------------------


class TaxonomyTerm(StrictModel):
    term: str = Field(min_length=1)
    synonyms: list[str] = Field(default_factory=list)


class Taxonomy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    taxonomy_version: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    # Lineage for derived versions; absent on the shipped base files. default_version
    # orders by created_at so a newer steward or evidence version always wins.
    parent_version: str | None = None
    created_at: str | None = None
    attribute_aliases: dict[str, list[str]] = Field(default_factory=dict)
    terms: list[TaxonomyTerm] = Field(default_factory=list)


class DocumentField(StrictModel):
    """One raw field of an uploaded/synthetic document (pre-alias mapping)."""

    value: str = Field(min_length=1)
    authority_rank: int = Field(default=3, ge=1)
    event_time: str = Field(min_length=1)
    verified: bool = False
    ttl_seconds: int = Field(default=3600, gt=0)


class Document(StrictModel):
    """One input document: raw fields, hash, dedup. Never trusted as-is."""

    document_id: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    source: str = Field(min_length=1)
    synthetic: bool = True
    trap: bool = False
    fields: dict[str, DocumentField] = Field(default_factory=dict)
    product_ref: str | None = None
    content_hash: str = ""


class BuildDataset(StrictModel):
    documents: list[Document] = Field(min_length=1)


class BuildRequest(StrictModel):
    work_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    dataset: BuildDataset
    expected_structure: dict[str, Any] | None = None


class BuildResponse(StrictModel):
    decision_id: str = Field(min_length=1)
    verdict: Literal["published", "escalated", "denied"]
    goldens: list[GoldenRecord] = Field(default_factory=list)
    conflicts: list[ConflictFact] = Field(default_factory=list)
    steward_queue: list[StewardItem] = Field(default_factory=list)
    policy_version_user: str | None = None  # legacy alias kept for the frontend
    policy_versions: dict[str, str] = Field(default_factory=dict)
    taxonomy_version: str = Field(min_length=1)
    splink_status: str = Field(min_length=1)


class JobStatus:
    """Job lifecycle. Terminal states never re-run without an explicit retry."""

    QUEUED = "queued"
    RUNNING = "running"
    PUBLISHED = "published"
    ESCALATED = "escalated"
    DENIED = "denied"
    FAILED = "failed"
    CANCELLED = "cancelled"

    TERMINAL = (PUBLISHED, ESCALATED, DENIED, FAILED, CANCELLED)


class JobResponse(StrictModel):
    """One background job: execution tracking only, never trust content."""

    job_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    status: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    attempts: int = Field(ge=0)
    tokens_used: int = Field(ge=0)
    result: dict[str, Any] | None = None
    error: str | None = None
    next_run_at: str | None = None
    created_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)


class UploadResponse(StrictModel):
    file_name: str = Field(min_length=1)
    detected_entity_type: str = Field(min_length=1)
    detected_confidence: Literal["high", "medium", "low"]
    fragments_parsed: int = Field(ge=0)
    fragments: list[dict[str, Any]] = Field(default_factory=list)
    fragments_rejected: list[dict[str, Any]] = Field(default_factory=list)
    link_proposals: list[ProposedLink] = Field(default_factory=list)
    uploaded: bool = True


class GraphNode(StrictModel):
    node_id: str = Field(min_length=1)
    node_type: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    entity_id: str = Field(min_length=1)
    source: str | None = None
    created_at: str = Field(min_length=1)
    decision_id: str | None = None


class GraphEdge(StrictModel):
    edge_id: str = Field(min_length=1)
    edge_type: str = Field(min_length=1)
    from_node_id: str = Field(min_length=1)
    to_node_id: str = Field(min_length=1)
    proof: str | None = None
    proposer: str | None = None
    rule_fired: str | None = None
    policy_version: str | None = None
    taxonomy_version: str | None = None
    decision_id: str | None = None


class GraphResponse(StrictModel):
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    path: list[str] = Field(default_factory=list)
    impact: dict[str, Any] = Field(default_factory=dict)


class DownstreamAnswer(StrictModel):
    """The two-answer contract: a golden, or No trusted value. No third answer."""

    answer: Literal["golden", "no_trusted_value"]
    agent: str = Field(min_length=1)
    entity_type: str = Field(pattern=ENTITY_TYPE_PATTERN)
    entity_id: str = Field(min_length=1)
    field: str | None = None
    value: str | None = None
    decision_id: str | None = None
    policy_version: str | None = None
    taxonomy_version: str | None = None
    lineage: str | None = None
    reason: str | None = None
    fallback: str | None = None


class StewardAcceptRequest(StrictModel):
    """Accept suggested taxonomy action. Bound to one exact action + version."""

    action: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
