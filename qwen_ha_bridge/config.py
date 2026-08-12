"""Environment-backed configuration with fail-closed safety invariants."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigurationError


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false", code="invalid_config")


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer", code="invalid_config") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum} and {maximum}", code="invalid_config"
        )
    return value


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number", code="invalid_config") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum} and {maximum}", code="invalid_config"
        )
    return value


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))


def _validated_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(
            "HA_BASE_URL must be an absolute http(s) URL", code="invalid_config"
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(
            "HA_BASE_URL must not contain credentials, query, or fragment", code="invalid_config"
        )
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Runtime configuration.

    Authentication is required by default. Write tools additionally require a
    non-empty allowlist; an unauthenticated deployment can never enable writes.
    """

    ha_base_url: str = "http://127.0.0.1:8123"
    ha_token_file: Path = Path("/opt/qwen-bridge/secrets/ha_token")
    bridge_api_key_file: Path = Path("/opt/qwen-bridge/secrets/bridge_api_key")
    auth_mode: str = "required"
    enable_write_tools: bool = False
    allowed_ha_services: frozenset[str] = frozenset()
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1")
    relaxed_sessions: bool = True
    request_body_limit: int = 65_536
    max_ha_response_bytes: int = 5_000_000
    max_service_data_bytes: int = 16_384
    max_state_results: int = 1_000
    max_concurrent_ha_requests: int = 8
    ha_timeout_seconds: float = 3.0
    keepalive_interval_seconds: float = 10.0
    session_ttl_seconds: int = 900
    max_sessions: int = 1_024
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        object.__setattr__(self, "ha_base_url", _validated_base_url(self.ha_base_url))
        if self.auth_mode not in {"required", "disabled"}:
            raise ConfigurationError(
                "BRIDGE_AUTH_MODE must be required or disabled", code="invalid_config"
            )
        if self.enable_write_tools and self.auth_mode != "required":
            raise ConfigurationError(
                "write tools require BRIDGE_AUTH_MODE=required", code="unsafe_config"
            )
        if self.enable_write_tools and not self.allowed_ha_services:
            raise ConfigurationError(
                "write tools require a non-empty ALLOWED_HA_SERVICES allowlist",
                code="unsafe_config",
            )
        for item in self.allowed_ha_services:
            parts = item.split(".")
            if len(parts) != 2 or not all(parts) or (parts[1] == "*" and not parts[0]):
                raise ConfigurationError(
                    f"invalid ALLOWED_HA_SERVICES entry: {item}", code="invalid_config"
                )
        if not self.allowed_hosts:
            raise ConfigurationError("ALLOWED_HOSTS cannot be empty", code="invalid_config")
        if not self.bind_host:
            raise ConfigurationError("BIND_HOST cannot be empty", code="invalid_config")

    @classmethod
    def from_env(cls) -> Settings:
        auth_mode = os.getenv("BRIDGE_AUTH_MODE", "required").strip().lower()
        services = frozenset(_csv(os.getenv("ALLOWED_HA_SERVICES", "")))
        hosts = _csv(os.getenv("ALLOWED_HOSTS", "localhost,127.0.0.1"))
        log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("invalid LOG_LEVEL", code="invalid_config")
        return cls(
            ha_base_url=_validated_base_url(os.getenv("HA_BASE_URL", "http://127.0.0.1:8123")),
            ha_token_file=Path(os.getenv("HA_TOKEN_FILE", "/opt/qwen-bridge/secrets/ha_token")),
            bridge_api_key_file=Path(
                os.getenv("BRIDGE_API_KEY_FILE", "/opt/qwen-bridge/secrets/bridge_api_key")
            ),
            auth_mode=auth_mode,
            enable_write_tools=_bool("ENABLE_WRITE_TOOLS", False),
            allowed_ha_services=services,
            bind_host=os.getenv("BIND_HOST", "127.0.0.1").strip(),
            bind_port=_int("BIND_PORT", 8000, minimum=1, maximum=65_535),
            allowed_hosts=hosts,
            relaxed_sessions=_bool("MCP_RELAXED_SESSIONS", True),
            request_body_limit=_int("REQUEST_BODY_LIMIT", 65_536, minimum=1_024, maximum=1_048_576),
            max_ha_response_bytes=_int(
                "MAX_HA_RESPONSE_BYTES", 5_000_000, minimum=65_536, maximum=50_000_000
            ),
            max_service_data_bytes=_int(
                "MAX_SERVICE_DATA_BYTES", 16_384, minimum=256, maximum=1_048_576
            ),
            max_state_results=_int("MAX_STATE_RESULTS", 1_000, minimum=1, maximum=10_000),
            max_concurrent_ha_requests=_int(
                "MAX_CONCURRENT_HA_REQUESTS", 8, minimum=1, maximum=100
            ),
            ha_timeout_seconds=_float("HA_TIMEOUT_SECONDS", 3.0, minimum=0.1, maximum=60.0),
            keepalive_interval_seconds=_float(
                "HA_KEEPALIVE_INTERVAL_SECONDS", 10.0, minimum=0.0, maximum=300.0
            ),
            session_ttl_seconds=_int("SESSION_TTL_SECONDS", 900, minimum=30, maximum=86_400),
            max_sessions=_int("MAX_SESSIONS", 1_024, minimum=1, maximum=100_000),
            log_level=log_level,
        )
