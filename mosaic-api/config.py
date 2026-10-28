"""mosaic-api configuration.

Environment variables only (CSD 12.3). No model ids, embedding model ids, or
thresholds are defaulted in code: if a value is required for the real demo and
is missing, startup fails rather than guessing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit


API_DIR = Path(__file__).resolve().parent
REPO_ROOT = API_DIR.parent
STATIC_DIR = REPO_ROOT / "web"


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
    embedding_model: str = ""
    embedding_version: str = ""
    embedding_dim: int | None = None
    trust_engine_url: str = "http://127.0.0.1:8080/derive"
    manglekit_bin: str = ""
    database_path: str = "./data/mosaic.db"
    mock_data_dir: Path = DEFAULT_MOCK_DATA_DIR
    static_dir: Path = STATIC_DIR

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env

        def get(name: str) -> str:
            return source.get(name, "").strip()

        trust_engine_url = get("TRUST_ENGINE_URL") or "http://127.0.0.1:8080/derive"
        parts = urlsplit(trust_engine_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ConfigurationError(
                "TRUST_ENGINE_URL must be an absolute http(s) URL, "
                f"got {trust_engine_url!r}"
            )

        muse_base_url = get("META_MODEL_API_BASE_URL") or "https://api.meta.ai/v1"

        return cls(
            use_mock_muse=_parse_bool(get("USE_MOCK_MUSE") or "true", "USE_MOCK_MUSE"),
            meta_model_api_key=get("META_MODEL_API_KEY"),
            meta_model_api_base_url=muse_base_url,
            muse_model_id=get("MUSE_MODEL_ID"),
            wonder_model_id=get("WONDER_MODEL_ID"),
            splink_enabled=_parse_bool(get("SPLINK_ENABLED") or "true", "SPLINK_ENABLED"),
            embedding_model=get("EMBEDDING_MODEL"),
            embedding_version=get("EMBEDDING_VERSION"),
            embedding_dim=_parse_optional_dim(get("EMBEDDING_DIM"), "EMBEDDING_DIM"),
            trust_engine_url=trust_engine_url,
            manglekit_bin=get("MANGLEKIT_BIN"),
            database_path=get("DATABASE_PATH") or "./data/mosaic.db",
        )

    @property
    def embedding_configured(self) -> bool:
        return bool(
            self.embedding_model
            and self.embedding_version
            and self.embedding_dim is not None
        )

    def health_url(self) -> str:
        """Return the trust-engine health URL derived from TRUST_ENGINE_URL."""
        parts = urlsplit(self.trust_engine_url)
        return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))

    def validate_startup(self) -> None:
        """Fail fast on incomplete configuration.

        Mock mode is allowed without a model key. Embedding configuration is
        all-or-nothing: partial configuration would risk mixing vectors from
        different models or versions, which CSD 5.3a forbids.
        """
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
            raise ConfigurationError(
                "Real Muse mode requires " + ", ".join(missing)
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()
