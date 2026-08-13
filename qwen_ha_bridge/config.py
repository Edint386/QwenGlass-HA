"""Environment-backed configuration with fail-closed safety invariants."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigurationError

_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_MAX_DEVICE_ALIASES = 64
_MAX_DEVICE_ALIAS_DESCRIPTION_CHARS = 1_800


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


def _device_aliases(raw: str) -> tuple[tuple[str, str], ...]:
    value = raw.strip()
    if not value:
        return ()
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigurationError(
            "HA_DEVICE_ALIASES_JSON must be valid JSON", code="invalid_config"
        ) from exc
    if not isinstance(payload, dict):
        raise ConfigurationError(
            "HA_DEVICE_ALIASES_JSON must be a JSON object", code="invalid_config"
        )
    if len(payload) > _MAX_DEVICE_ALIASES:
        raise ConfigurationError(
            f"HA_DEVICE_ALIASES_JSON supports at most {_MAX_DEVICE_ALIASES} aliases",
            code="invalid_config",
        )

    aliases: list[tuple[str, str]] = []
    description_chars = 0
    for raw_alias, raw_entity_id in payload.items():
        if not isinstance(raw_alias, str) or not isinstance(raw_entity_id, str):
            raise ConfigurationError(
                "HA_DEVICE_ALIASES_JSON keys and values must be strings",
                code="invalid_config",
            )
        alias = raw_alias.strip()
        entity_id = raw_entity_id.strip()
        if not alias or len(alias) > 64 or any(ord(character) < 32 for character in alias):
            raise ConfigurationError(
                f"invalid HA device alias: {raw_alias!r}", code="invalid_config"
            )
        if not _ENTITY_ID.fullmatch(entity_id):
            raise ConfigurationError(
                f"invalid HA device alias entity: {raw_entity_id!r}", code="invalid_config"
            )
        description_chars += len(alias) + len(entity_id) + 4
        aliases.append((alias, entity_id))

    if description_chars > _MAX_DEVICE_ALIAS_DESCRIPTION_CHARS:
        raise ConfigurationError(
            "HA_DEVICE_ALIASES_JSON is too large for a concise MCP tool description",
            code="invalid_config",
        )
    return tuple(aliases)


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
    allowed_ha_entities: frozenset[str] = frozenset()
    ha_device_aliases: tuple[tuple[str, str], ...] = ()
    climate_temperature_min: float = 16.0
    climate_temperature_max: float = 30.0
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
    control_total_timeout_seconds: float = 1.8
    control_read_timeout_seconds: float = 0.25
    control_write_timeout_seconds: float = 1.0
    control_settle_seconds: float = 0.05
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
        if self.enable_write_tools and not self.allowed_ha_entities:
            raise ConfigurationError(
                "write tools require a non-empty ALLOWED_HA_ENTITIES allowlist",
                code="unsafe_config",
            )
        for item in self.allowed_ha_services:
            parts = item.split(".")
            if len(parts) != 2 or not all(parts) or (parts[1] == "*" and not parts[0]):
                raise ConfigurationError(
                    f"invalid ALLOWED_HA_SERVICES entry: {item}", code="invalid_config"
                )
        for entity_id in self.allowed_ha_entities:
            if not _ENTITY_ID.fullmatch(entity_id):
                raise ConfigurationError(
                    f"invalid ALLOWED_HA_ENTITIES entry: {entity_id}", code="invalid_config"
                )
        alias_targets = {entity_id for _, entity_id in self.ha_device_aliases}
        unlisted_alias_targets = sorted(alias_targets - self.allowed_ha_entities)
        if unlisted_alias_targets:
            raise ConfigurationError(
                "HA_DEVICE_ALIASES_JSON targets must be present in ALLOWED_HA_ENTITIES: "
                + ", ".join(unlisted_alias_targets),
                code="unsafe_config",
            )
        if self.climate_temperature_min >= self.climate_temperature_max:
            raise ConfigurationError(
                "CLIMATE_TEMPERATURE_MIN must be below CLIMATE_TEMPERATURE_MAX",
                code="invalid_config",
            )
        if (
            self.control_total_timeout_seconds <= 0
            or self.control_read_timeout_seconds <= 0
            or self.control_write_timeout_seconds <= 0
            or self.control_settle_seconds < 0
        ):
            raise ConfigurationError(
                "HA control timeouts must be positive (settle may be zero)",
                code="invalid_config",
            )
        if self.control_total_timeout_seconds > 2.0:
            raise ConfigurationError(
                "HA_CONTROL_TOTAL_TIMEOUT_SECONDS must be at most 2.0",
                code="invalid_config",
            )
        control_phase_budget = (
            (self.control_read_timeout_seconds * 2)
            + self.control_write_timeout_seconds
            + self.control_settle_seconds
        )
        if control_phase_budget > self.control_total_timeout_seconds:
            raise ConfigurationError(
                "HA control phase timeouts must fit within HA_CONTROL_TOTAL_TIMEOUT_SECONDS",
                code="invalid_config",
            )
        if not self.allowed_hosts:
            raise ConfigurationError("ALLOWED_HOSTS cannot be empty", code="invalid_config")
        if not self.bind_host:
            raise ConfigurationError("BIND_HOST cannot be empty", code="invalid_config")

    @classmethod
    def from_env(cls) -> Settings:
        auth_mode = os.getenv("BRIDGE_AUTH_MODE", "required").strip().lower()
        services = frozenset(_csv(os.getenv("ALLOWED_HA_SERVICES", "")))
        entities = frozenset(_csv(os.getenv("ALLOWED_HA_ENTITIES", "")))
        aliases = _device_aliases(os.getenv("HA_DEVICE_ALIASES_JSON", ""))
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
            allowed_ha_entities=entities,
            ha_device_aliases=aliases,
            climate_temperature_min=_float(
                "CLIMATE_TEMPERATURE_MIN", 16.0, minimum=-50.0, maximum=100.0
            ),
            climate_temperature_max=_float(
                "CLIMATE_TEMPERATURE_MAX", 30.0, minimum=-50.0, maximum=100.0
            ),
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
            control_total_timeout_seconds=_float(
                "HA_CONTROL_TOTAL_TIMEOUT_SECONDS", 1.8, minimum=0.2, maximum=2.0
            ),
            control_read_timeout_seconds=_float(
                "HA_CONTROL_READ_TIMEOUT_SECONDS", 0.25, minimum=0.05, maximum=5.0
            ),
            control_write_timeout_seconds=_float(
                "HA_CONTROL_WRITE_TIMEOUT_SECONDS", 1.0, minimum=0.05, maximum=5.0
            ),
            control_settle_seconds=_float(
                "HA_CONTROL_SETTLE_SECONDS", 0.05, minimum=0.0, maximum=1.0
            ),
            keepalive_interval_seconds=_float(
                "HA_KEEPALIVE_INTERVAL_SECONDS", 10.0, minimum=0.0, maximum=300.0
            ),
            session_ttl_seconds=_int("SESSION_TTL_SECONDS", 900, minimum=30, maximum=86_400),
            max_sessions=_int("MAX_SESSIONS", 1_024, minimum=1, maximum=100_000),
            log_level=log_level,
        )
