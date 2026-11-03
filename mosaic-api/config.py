"""mosaic-api configuration.

Environment variables only (CSD 12.3). No model ids, embedding model ids, or
thresholds are defaulted in code: if a value is required for the real demo and
is missing, startup fails rather than guessing.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

API_DIR = Path(__file__).resolve().parent
REPO_ROOT = API_DIR.parent


def _default_static_dir() -> Path:
    """Locate the static UI in either layout.

    Repository checkout: <repo>/web (API_DIR.parent).
    Docker image: mosaic-api/ is flattened into /app, so the UI lives at
    API_DIR/web. Prefer the checkout layout when it exists.
    """
    checkout = REPO_ROOT / "web"
    if checkout.is_dir():
        return checkout
    return API_DIR / "web"


STATIC_DIR = _default_static_dir()


def _default_mock_data_dir() -> Path:
    """Locate the synthetic fixtures in either layout.

    Repository checkout: <repo>/data/mock (API_DIR.parent).
    Docker image: mosaic-api/ is flattened into /app, so fixtures live at
    API_DIR/data/mock. Prefer the checkout layout when it exists.
    """
    checkout = REPO_ROOT / "data" / "mock"
    if checkout.is_dir():
        return checkout
    return API_DIR / "data" / "mock"


DEFAULT_MOCK_DATA_DIR = _default_mock_data_dir()

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigurationError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _parse_bool(raw: str, name: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigurationError(f"{name} must be a boolean (true/false), got {raw!r}")


def _parse_optional_int(raw: str, name: str, minimum: int = 0) -> int:
    value = raw.strip()
    if not value:
        return 0
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if parsed < minimum:
        raise ConfigurationError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed


def _parse_optional_dim(raw: str, name: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
    if parsed <= 0:
        raise ConfigurationError(f"{name} must be greater than zero, got {parsed}")
    return parsed


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the mosaic-api service."""

    use_mock_muse: bool = True
    meta_model_api_key: str = ""
    meta_model_api_base_url: str = ""
    muse_model_id: str = ""
    wonder_model_id: str = ""
    splink_enabled: bool = True
    matching_service_url: str = "http://127.0.0.1:8081"
    embedding_model: str = ""
    embedding_version: str = ""
    embedding_dim: int | None = None
    trust_engine_url: str = "http://127.0.0.1:8080/derive"
    manglekit_bin: str = ""
    database_path: str = "./data/mosaic.db"
    mock_data_dir: Path = DEFAULT_MOCK_DATA_DIR
    static_dir: Path = STATIC_DIR
    autonomy_mode: str = "manual"
    # Opt-in: let Muse propose near-match cluster merges that exact identifiers
    # miss (see muse_candidates.py). Off by default: it changes cluster counts.
    muse_candidate_matching: bool = False
    # Extend the product taxonomy with evidence-verified terms before deriving:
    # "off" (default), "rules" (no model) or "muse" (a model proposes; the same
    # evidence checks decide). Never auto-accepts on a model's say-so.
    taxonomy_proposals: str = "off"
    taxonomy_min_support: int = 3
    # Lowest judge confidence that may merge two clusters: "high" (default) or "medium".
    muse_candidate_min_confidence: str = "high"
    # Judge budget for candidate matching. 0 = no cap: at 10k-100k clusters the
    # shortlist stays bounded by per_cluster, and the cap only hid pairs.
    muse_candidate_max_pairs: int = 0
    muse_candidate_per_cluster: int = 3
    muse_judge_concurrency: int = 8  # parallel judge batches per build
    muse_verdict_cache: bool = True  # reuse verdicts for identical name+address pairs
    # "token" (default, single name tokens) or "rarest_pair" (also token pairs + address).
    muse_blocking_mode: str = "token"
    muse_blocking_max_block: int = 0  # 0 = built-in default (50 clusters per blocking key)
    # When set, every state-changing request (POST/PUT/PATCH/DELETE) must carry
    # "Authorization: Bearer <token>". Empty = open (local dev only).
    api_token: str = ""
    job_max_attempts: int = 3
    muse_job_token_budget: int = 0
    muse_daily_token_budget: int = 0
    inbox_dir: str = ""
    inbox_poll_seconds: int = 30
    analyser_interval_seconds: int = 0
    steward_webhook_url: str = ""
    steward_webhook_secret: str = ""
    ingest_webhook_secret: str = ""
    enable_background_tasks: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if env is None else env

        def get(name: str) -> str:
            return source.get(name, "").strip()

        trust_engine_url = get("TRUST_ENGINE_URL") or "http://127.0.0.1:8080/derive"
        parts = urlsplit(trust_engine_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ConfigurationError(
                "TRUST_ENGINE_URL must be an absolute http(s) URL, " f"got {trust_engine_url!r}"
            )

        muse_base_url = get("META_MODEL_API_BASE_URL") or "https://api.meta.ai/v1"

        return cls(
            use_mock_muse=_parse_bool(get("USE_MOCK_MUSE") or "true", "USE_MOCK_MUSE"),
            meta_model_api_key=get("META_MODEL_API_KEY"),
            meta_model_api_base_url=muse_base_url,
            muse_model_id=get("MUSE_MODEL_ID"),
            wonder_model_id=get("WONDER_MODEL_ID"),
            splink_enabled=_parse_bool(get("SPLINK_ENABLED") or "true", "SPLINK_ENABLED"),
            matching_service_url=get("MATCHING_ENGINE_URL") or "http://127.0.0.1:8081",
            embedding_model=get("EMBEDDING_MODEL"),
            embedding_version=get("EMBEDDING_VERSION"),
            embedding_dim=_parse_optional_dim(get("EMBEDDING_DIM"), "EMBEDDING_DIM"),
            trust_engine_url=trust_engine_url,
            manglekit_bin=get("MANGLEKIT_BIN"),
            database_path=get("DATABASE_PATH") or "./data/mosaic.db",
            api_token=get("MOSAIC_API_TOKEN").strip(),
            taxonomy_proposals=(get("MUSE_TAXONOMY_PROPOSALS") or "off").strip().lower(),
            taxonomy_min_support=_parse_optional_int(
                get("TAXONOMY_MIN_SUPPORT"), "TAXONOMY_MIN_SUPPORT", minimum=1
            )
            or 3,
            muse_candidate_min_confidence=(get("MUSE_CANDIDATE_MIN_CONFIDENCE") or "high")
            .strip()
            .lower(),
            muse_candidate_matching=_parse_bool(
                get("MUSE_CANDIDATE_MATCHING") or "false", "MUSE_CANDIDATE_MATCHING"
            ),
            muse_candidate_max_pairs=_parse_optional_int(
                get("MUSE_CANDIDATE_MAX_PAIRS"), "MUSE_CANDIDATE_MAX_PAIRS", minimum=0
            )
            or 0,
            muse_candidate_per_cluster=_parse_optional_int(
                get("MUSE_CANDIDATE_PER_CLUSTER"), "MUSE_CANDIDATE_PER_CLUSTER", minimum=1
            )
            or 3,
            muse_judge_concurrency=_parse_optional_int(
                get("MUSE_JUDGE_CONCURRENCY"), "MUSE_JUDGE_CONCURRENCY", minimum=1
            )
            or 8,
            muse_verdict_cache=_parse_bool(
                get("MUSE_VERDICT_CACHE") or "true", "MUSE_VERDICT_CACHE"
            ),
            muse_blocking_mode=(get("MUSE_BLOCKING_MODE") or "token").strip().lower(),
            muse_blocking_max_block=_parse_optional_int(
                get("MUSE_BLOCKING_MAX_BLOCK"), "MUSE_BLOCKING_MAX_BLOCK", minimum=0
            )
            or 0,
            autonomy_mode=(get("AUTONOMY_MODE") or "manual").strip().lower(),
            job_max_attempts=_parse_optional_int(
                get("JOB_MAX_ATTEMPTS"), "JOB_MAX_ATTEMPTS", minimum=1
            )
            or 3,
            muse_job_token_budget=_parse_optional_int(
                get("MUSE_JOB_TOKEN_BUDGET"), "MUSE_JOB_TOKEN_BUDGET"
            ),
            muse_daily_token_budget=_parse_optional_int(
                get("MUSE_DAILY_TOKEN_BUDGET"), "MUSE_DAILY_TOKEN_BUDGET"
            ),
            inbox_dir=get("INBOX_DIR"),
            inbox_poll_seconds=_parse_optional_int(
                get("INBOX_POLL_SECONDS"), "INBOX_POLL_SECONDS", minimum=1
            )
            or 30,
            analyser_interval_seconds=_parse_optional_int(
                get("ANALYSER_INTERVAL_SECONDS"), "ANALYSER_INTERVAL_SECONDS"
            ),
            steward_webhook_url=get("STEWARD_WEBHOOK_URL"),
            steward_webhook_secret=get("STEWARD_WEBHOOK_SECRET"),
            ingest_webhook_secret=get("INGEST_WEBHOOK_SECRET"),
            enable_background_tasks=_parse_bool(
                get("ENABLE_BACKGROUND_TASKS") or "true", "ENABLE_BACKGROUND_TASKS"
            ),
        )

    @property
    def embedding_configured(self) -> bool:
        return bool(
            self.embedding_model and self.embedding_version and self.embedding_dim is not None
        )

    def health_url(self) -> str:
        """Return the trust-engine health URL derived from TRUST_ENGINE_URL."""
        parts = urlsplit(self.trust_engine_url)
        return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))

    def matching_health_url(self) -> str:
        """Return the matching-engine health URL derived from MATCHING_ENGINE_URL."""
        parts = urlsplit(self.matching_service_url)
        return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))

    def validate_startup(self) -> None:
        """Fail fast on incomplete configuration.

        Mock mode is allowed without a model key. Embedding configuration is
        all-or-nothing: partial configuration would risk mixing vectors from
        different models or versions, which CSD 5.3a forbids.
        """
        if self.taxonomy_proposals not in ("off", "rules", "muse"):
            raise ConfigurationError(
                f"MUSE_TAXONOMY_PROPOSALS must be off, rules or muse, got {self.taxonomy_proposals!r}"
            )
        if self.muse_blocking_mode not in ("token", "rarest_pair"):
            raise ConfigurationError(
                f"MUSE_BLOCKING_MODE must be token or rarest_pair, got {self.muse_blocking_mode!r}"
            )
        if self.muse_candidate_min_confidence not in ("high", "medium"):
            raise ConfigurationError(
                "MUSE_CANDIDATE_MIN_CONFIDENCE must be high or medium, "
                f"got {self.muse_candidate_min_confidence!r}"
            )
        if self.autonomy_mode not in ("manual", "supervised"):
            raise ConfigurationError(
                f"AUTONOMY_MODE must be manual or supervised, got {self.autonomy_mode!r}"
            )
        if not self.use_mock_muse:
            missing = [
                name
                for name, value in (
                    ("META_MODEL_API_KEY", self.meta_model_api_key),
                    ("MUSE_MODEL_ID", self.muse_model_id),
                    ("META_MODEL_API_BASE_URL", self.meta_model_api_base_url),
                )
                if not value
            ]
            if missing:
                raise ConfigurationError(
                    "USE_MOCK_MUSE=false requires "
                    + ", ".join(missing)
                    + "; refusing to guess a model id or run without a key"
                )
            base = urlsplit(self.meta_model_api_base_url)
            if base.scheme != "https" or not base.netloc:
                raise ConfigurationError(
                    "META_MODEL_API_BASE_URL must be an absolute https URL, "
                    f"got {self.meta_model_api_base_url!r}"
                )

        embedding_parts = (
            bool(self.embedding_model),
            bool(self.embedding_version),
            self.embedding_dim is not None,
        )
        if any(embedding_parts) and not all(embedding_parts):
            raise ConfigurationError(
                "EMBEDDING_MODEL, EMBEDDING_VERSION and EMBEDDING_DIM must be "
                "configured together; partial embedding configuration is unsafe"
            )

    def require_embedding_config(self) -> None:
        """Raise if embeddings are not fully pinned."""
        if not self.embedding_configured:
            raise ConfigurationError(
                "Embedding configuration is not pinned. Set EMBEDDING_MODEL, "
                "EMBEDDING_VERSION and EMBEDDING_DIM before using similar search "
                "or writing profile embeddings."
            )

    def require_real_muse_config(self) -> None:
        """Raise if real Muse mode is selected without its required values."""
        if self.use_mock_muse:
            return
        missing = [
            name
            for name, value in (
                ("META_MODEL_API_KEY", self.meta_model_api_key),
                ("META_MODEL_API_BASE_URL", self.meta_model_api_base_url),
                ("MUSE_MODEL_ID", self.muse_model_id),
            )
            if not value
        ]
        if missing:
            raise ConfigurationError("Real Muse mode requires " + ", ".join(missing))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
