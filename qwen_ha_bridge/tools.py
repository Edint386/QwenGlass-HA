"""MCP tool catalog, argument validation, and Home Assistant policy checks."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import BridgeError, ToolInputError, ToolPermissionError
from .ha_client import HomeAssistantClient

logger = logging.getLogger(__name__)

_HA_NAME = re.compile(r"^[a-z0-9_]+$")
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, bool]

    def as_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }


class ToolRegistry:
    def __init__(self, settings: Settings, ha: HomeAssistantClient) -> None:
        self.settings = settings
        self.ha = ha

    def definitions(self) -> list[dict[str, Any]]:
        definitions = [
            ToolDefinition(
                name="bridge_status",
                description="Check whether the bridge can authenticate to Home Assistant.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            ToolDefinition(
                name="list_ha_states",
                description=(
                    "List Home Assistant entity IDs, current states, and friendly names. "
                    "Results can be filtered and paginated."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "domain": {
                            "type": "string",
                            "description": "Optional entity domain such as light or sensor.",
                        },
                        "offset": {"type": "integer", "minimum": 0, "default": 0},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": self.settings.max_state_results,
                            "default": min(500, self.settings.max_state_results),
                        },
                    },
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
            ToolDefinition(
                name="get_ha_state",
                description="Get the complete state and attributes of one Home Assistant entity.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "entity_id": {
                            "type": "string",
                            "description": "Entity ID such as light.living_room.",
                            "pattern": "^[a-z0-9_]+\\.[a-z0-9_]+$",
                        }
                    },
                    "required": ["entity_id"],
                    "additionalProperties": False,
                },
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                    "openWorldHint": False,
                },
            ),
        ]
        if self.settings.enable_write_tools:
            definitions.append(
                ToolDefinition(
                    name="call_ha_service",
                    description=(
                        "Call one explicitly allowlisted Home Assistant service. "
                        "Use dry_run=true to validate without operating a device."
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "domain": {"type": "string", "pattern": "^[a-z0-9_]+$"},
                            "service": {"type": "string", "pattern": "^[a-z0-9_]+$"},
                            "service_data": {
                                "type": "object",
                                "description": "Home Assistant service data object.",
                            },
                            "service_data_json": {
                                "type": "string",
                                "description": "Legacy JSON-string form of service_data.",
                            },
                            "dry_run": {"type": "boolean", "default": False},
                        },
                        "required": ["domain", "service"],
                        "additionalProperties": False,
                    },
                    annotations={
                        "readOnlyHint": False,
                        "destructiveHint": True,
                        "idempotentHint": False,
                        "openWorldHint": False,
                    },
                )
            )
        return [definition.as_mcp() for definition in definitions]

    async def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        if not isinstance(name, str) or not name:
            raise ToolInputError("tool name must be a non-empty string", code="invalid_tool")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolInputError("tool arguments must be an object", code="invalid_arguments")
        started = time.perf_counter()
        try:
            if name == "bridge_status":
                self._only_keys(arguments, set())
                return await self.ha.health()
            if name == "list_ha_states":
                return await self._list_states(arguments)
            if name == "get_ha_state":
                return await self._get_state(arguments)
            if name == "call_ha_service":
                return await self._call_service(arguments)
            raise ToolInputError(f"unknown tool: {name}", code="unknown_tool")
        finally:
            logger.info(
                "tool completed name=%r duration_ms=%.1f",
                name,
                (time.perf_counter() - started) * 1000,
            )

    @staticmethod
    def _only_keys(arguments: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            raise ToolInputError(
                f"unexpected argument keys: {', '.join(unknown)}", code="invalid_arguments"
            )

    @staticmethod
    def _bounded_int(value: Any, *, name: str, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolInputError(f"{name} must be an integer", code="invalid_arguments")
        if not minimum <= value <= maximum:
            raise ToolInputError(
                f"{name} must be between {minimum} and {maximum}", code="invalid_arguments"
            )
        return value

    async def _list_states(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._only_keys(arguments, {"domain", "offset", "limit"})
        domain = arguments.get("domain")
        if domain is not None and (not isinstance(domain, str) or not _HA_NAME.fullmatch(domain)):
            raise ToolInputError("domain is invalid", code="invalid_arguments")
        offset = self._bounded_int(
            arguments.get("offset", 0), name="offset", minimum=0, maximum=1_000_000
        )
        limit = self._bounded_int(
            arguments.get("limit", min(500, self.settings.max_state_results)),
            name="limit",
            minimum=1,
            maximum=self.settings.max_state_results,
        )
        states = await self.ha.states()
        if domain:
            prefix = f"{domain}."
            states = [item for item in states if str(item.get("entity_id", "")).startswith(prefix)]
        compact = [
            {
                "entity_id": item.get("entity_id"),
                "state": item.get("state"),
                "friendly_name": (
                    item.get("attributes", {}).get("friendly_name")
                    if isinstance(item.get("attributes"), dict)
                    else None
                ),
            }
            for item in states[offset : offset + limit]
        ]
        next_offset = offset + len(compact)
        return {
            "total": len(states),
            "count": len(compact),
            "offset": offset,
            "next_offset": next_offset if next_offset < len(states) else None,
            "states": compact,
        }

    async def _get_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._only_keys(arguments, {"entity_id"})
        entity_id = arguments.get("entity_id")
        if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
            raise ToolInputError("entity_id is invalid", code="invalid_arguments")
        return await self.ha.state(entity_id)

    def _service_allowed(self, domain: str, service: str) -> bool:
        return (
            f"{domain}.{service}" in self.settings.allowed_ha_services
            or f"{domain}.*" in self.settings.allowed_ha_services
        )

    async def _call_service(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.settings.enable_write_tools:
            raise ToolPermissionError("write tools are disabled", code="writes_disabled")
        self._only_keys(
            arguments, {"domain", "service", "service_data", "service_data_json", "dry_run"}
        )
        domain = arguments.get("domain")
        service = arguments.get("service")
        if not isinstance(domain, str) or not _HA_NAME.fullmatch(domain):
            raise ToolInputError("domain is invalid", code="invalid_arguments")
        if not isinstance(service, str) or not _HA_NAME.fullmatch(service):
            raise ToolInputError("service is invalid", code="invalid_arguments")
        if not self._service_allowed(domain, service):
            raise ToolPermissionError(
                f"service {domain}.{service} is not allowlisted", code="service_not_allowed"
            )
        if "service_data" in arguments and "service_data_json" in arguments:
            raise ToolInputError(
                "provide service_data or service_data_json, not both", code="invalid_arguments"
            )
        payload: Any = arguments.get("service_data", {})
        if "service_data_json" in arguments:
            raw = arguments["service_data_json"]
            if not isinstance(raw, str):
                raise ToolInputError("service_data_json must be a string", code="invalid_arguments")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ToolInputError(
                    "service_data_json is invalid JSON", code="invalid_arguments"
                ) from exc
        if not isinstance(payload, dict):
            raise ToolInputError("service_data must be an object", code="invalid_arguments")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(encoded) > self.settings.max_service_data_bytes:
            raise ToolInputError("service_data is too large", code="invalid_arguments")
        dry_run = arguments.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise ToolInputError("dry_run must be a boolean", code="invalid_arguments")
        if dry_run:
            return {
                "dry_run": True,
                "allowed": True,
                "service": f"{domain}.{service}",
                "service_data_keys": sorted(payload),
            }
        return await self.ha.call_service(domain, service, payload)


def tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Encode a successful tool result in legacy and structured MCP forms."""
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": payload,
        "isError": False,
    }


def tool_error(error: Exception) -> dict[str, Any]:
    if isinstance(error, BridgeError):
        payload = {"error": error.message, "code": error.code}
    else:
        logger.exception("unexpected tool failure")
        payload = {"error": "internal bridge error", "code": "internal_error"}
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            }
        ],
        "structuredContent": payload,
        "isError": True,
    }
