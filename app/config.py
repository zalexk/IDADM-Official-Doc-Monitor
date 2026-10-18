"""Application settings and document registry loading."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_VERSION = "0.1.0"
DEFAULT_USER_AGENT = (
    "IDADM-Official-Doc-Monitor/0.1 "
    "(low-frequency polling of public PDFs; contact: admin@example.com)"
)


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded or is invalid."""


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class DocumentConfig:
    key: str
    title: str
    url: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    documents_config: Path
    database_path: Path
    scheduler_enabled: bool = True
    check_on_start: bool = True
    check_interval_minutes: int = 30
    full_check_interval_hours: int = 24
    http_timeout_seconds: float = 60.0
    max_download_bytes: int = 25 * 1024 * 1024
    api_key: str | None = None
    user_agent: str = DEFAULT_USER_AGENT
    log_level: str = "INFO"
    max_diff_chars: int = 250_000

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser().resolve()
        documents_config = Path(
            os.getenv("DOCUMENTS_CONFIG", str(PROJECT_ROOT / "config" / "documents.yaml"))
        ).expanduser().resolve()
        database_raw = os.getenv("DATABASE_PATH", "").strip()
        database_path = (
            Path(database_raw).expanduser().resolve()
            if database_raw
            else data_dir / "monitor.db"
        )
        api_key = os.getenv("API_KEY", "").strip() or None
        interval = _env_int("CHECK_INTERVAL_MINUTES", 30, minimum=1)
        full_interval = _env_int("FULL_CHECK_INTERVAL_HOURS", 24, minimum=0)
        timeout = _env_float("HTTP_TIMEOUT_SECONDS", 60.0, minimum=1.0)
        max_bytes = _env_int("MAX_DOWNLOAD_BYTES", 25 * 1024 * 1024, minimum=1024)
        max_diff = _env_int("MAX_DIFF_CHARS", 250_000, minimum=10_000)
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        return cls(
            data_dir=data_dir,
            documents_config=documents_config,
            database_path=database_path,
            scheduler_enabled=_env_bool("SCHEDULER_ENABLED", True),
            check_on_start=_env_bool("CHECK_ON_START", True),
            check_interval_minutes=interval,
            full_check_interval_hours=full_interval,
            http_timeout_seconds=timeout,
            max_download_bytes=max_bytes,
            api_key=api_key,
            user_agent=os.getenv("USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT,
            log_level=log_level,
            max_diff_chars=max_diff,
        )

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)


_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def load_documents(path: Path) -> tuple[DocumentConfig, ...]:
    if not path.exists():
        raise ConfigError(f"Documents config not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict) or not isinstance(raw.get("documents"), list):
        raise ConfigError(f"{path} must contain a top-level 'documents' list")
    if not raw["documents"]:
        raise ConfigError(f"{path} contains no documents")

    documents: list[DocumentConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw["documents"], start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"documents[{index}] must be a mapping")
        key = str(item.get("key", "")).strip()
        title = str(item.get("title", "")).strip()
        url = str(item.get("url", "")).strip()
        if not _KEY_RE.match(key):
            raise ConfigError(
                f"documents[{index}].key must match {_KEY_RE.pattern}, got {key!r}"
            )
        if key in seen:
            raise ConfigError(f"Duplicate document key: {key}")
        if not title:
            raise ConfigError(f"documents[{index}].title is required")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"documents[{index}].url must be an absolute HTTP(S) URL")
        tags_raw = item.get("tags", [])
        if not isinstance(tags_raw, list):
            raise ConfigError(f"documents[{index}].tags must be a list")
        tags = tuple(str(tag).strip() for tag in tags_raw if str(tag).strip())
        documents.append(DocumentConfig(key=key, title=title, url=url, tags=tags))
        seen.add(key)
    return tuple(documents)
