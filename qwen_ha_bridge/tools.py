"""MCP tool catalog, argument validation, and Home Assistant policy checks."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import BridgeError, HomeAssistantError, ToolInputError, ToolPermissionError
from .ha_client import HomeAssistantClient
from .middleware import current_request_id

logger = logging.getLogger(__name__)

_HA_NAME = re.compile(r"^[a-z0-9_]+$")
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_ACTION_UNCONFIRMED_MESSAGE = (
    "Home Assistant may have received the action, but the resulting state was not confirmed. "
    "Do not report success or failure and do not retry the write automatically."
)


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
                    "Results can be filtered and paginated. Before controlling a device, "
                    "use this tool when its name or room does not resolve to one configured alias."
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
                    description=self._write_tool_description(),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "domain": self._domain_schema(),
                            "service": self._service_schema(),
                            "service_data": self._service_data_schema(),
                            "service_data_json": {
                                "type": "string",
                                "description": (
                                    "Legacy JSON-string form of service_data. It must contain the "
                                    "same exact allowlisted entity_id and obey the same limits."
                                ),
                            },
                            "dry_run": {
                                "type": "boolean",
                                "default": False,
                                "description": (
                                    "Use true only to validate a planned call without operating a "
                                    "device. Use false for an explicit user control request."
                                ),
                            },
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

    def _alias_summary(self) -> str:
        grouped: dict[str, list[str]] = {}
        for alias, entity_id in self.settings.ha_device_aliases:
            grouped.setdefault(entity_id, []).append(alias)
        return "; ".join(
            f"{'/'.join(aliases)} -> {entity_id}" for entity_id, aliases in grouped.items()
        )

    def _write_tool_description(self) -> str:
        services = ", ".join(sorted(self.settings.allowed_ha_services))
        alias_rule = (
            "Configured aliases are listed once in service_data.entity_id. "
            if self.settings.ha_device_aliases
            else "No aliases are configured; resolve the exact entity with list_ha_states. "
        )
        return (
            "Control exactly one allowlisted Home Assistant entity using one allowlisted service. "
            "Pass the domain separately and pass only the unqualified service name in `service` "
            "(for example, domain=climate with service=set_temperature; never put "
            "`climate.set_temperature` in `service`). "
            f"Allowed domain.service pairs: {services}. {alias_rule}"
            "For Chinese commands, 打开/关闭 maps to service=turn_on/turn_off; 调到 N 度 maps "
            "to domain=climate, service=set_temperature with temperature=N; 亮度 N% maps to "
            "domain=light, service=turn_on with brightness_pct=N. If a device reference such "
            "as `灯` matches more than one entity, "
            "call list_ha_states and ask which one instead of guessing. Always pass "
            "service_data.entity_id. Use dry_run=true only for validation."
        )

    def _domain_schema(self) -> dict[str, Any]:
        domains = sorted(service.split(".", 1)[0] for service in self.settings.allowed_ha_services)
        return {
            "type": "string",
            "enum": sorted(set(domains)),
            "description": "Home Assistant service domain allowed by this private bridge.",
        }

    def _service_schema(self) -> dict[str, Any]:
        services = sorted(
            {service.split(".", 1)[1] for service in self.settings.allowed_ha_services}
        )
        return {
            "type": "string",
            "enum": services,
            "description": (
                "Unqualified service name only. Do not include the domain or a dot. "
                "Valid domain.service pairs: "
                + ", ".join(sorted(self.settings.allowed_ha_services))
            ),
        }

    def _service_data_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "description": (
                "Parameters for one device. entity_id is mandatory and must be one of the "
                "configured entities. Alternate HA targets such as area_id or device_id are "
                "rejected."
            ),
            "properties": {
                "entity_id": {
                    "type": "string",
                    "enum": sorted(self.settings.allowed_ha_entities),
                    "description": self._alias_summary() or "An exact allowlisted entity ID.",
                },
                "temperature": {
                    "type": "number",
                    "minimum": self.settings.climate_temperature_min,
                    "maximum": self.settings.climate_temperature_max,
                    "description": "Target temperature in degrees Celsius.",
                },
                "brightness_pct": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Optional light brightness percentage.",
                },
            },
            "required": ["entity_id"],
            "additionalProperties": True,
        }

    async def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        if not isinstance(name, str) or not name:
            raise ToolInputError("tool name must be a non-empty string", code="invalid_tool")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            raise ToolInputError("tool arguments must be an object", code="invalid_arguments")
        started = time.perf_counter()
        outcome = "success"
        error_code: str | None = None
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
        except BridgeError as exc:
            outcome = "error"
            error_code = exc.code
            raise
        except Exception:
            outcome = "error"
            error_code = "internal_error"
            raise
        finally:
            logger.info(
                "%s",
                json.dumps(
                    {
                        "event": "tool_completed",
                        "request_id": current_request_id(),
                        "tool": name,
                        "is_error": outcome == "error",
                        "error_code": error_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
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

    @staticmethod
    def _only_service_data_keys(payload: dict[str, Any], allowed: set[str]) -> None:
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ToolInputError(
                f"unexpected service_data keys: {', '.join(unknown)}",
                code="invalid_arguments",
            )

    def _validate_service_payload(self, domain: str, service: str, payload: dict[str, Any]) -> str:
        alternate_targets = sorted({"area_id", "device_id", "label_id"} & set(payload))
        if alternate_targets:
            raise ToolInputError(
                "alternate Home Assistant targets are not allowed: " + ", ".join(alternate_targets),
                code="invalid_arguments",
            )
        entity_id = payload.get("entity_id")
        if not isinstance(entity_id, str) or not _ENTITY_ID.fullmatch(entity_id):
            raise ToolInputError(
                "service_data.entity_id must be one exact entity ID",
                code="invalid_arguments",
            )
        if entity_id not in self.settings.allowed_ha_entities:
            raise ToolPermissionError(
                f"entity {entity_id} is not allowlisted", code="entity_not_allowed"
            )
        entity_domain = entity_id.split(".", 1)[0]
        if entity_domain != domain:
            raise ToolInputError(
                f"service domain {domain} does not match entity domain {entity_domain}",
                code="invalid_arguments",
            )

        service_name = f"{domain}.{service}"
        if service_name in {"climate.turn_on", "climate.turn_off", "light.turn_off"}:
            self._only_service_data_keys(payload, {"entity_id"})
        elif service_name == "climate.set_temperature":
            self._only_service_data_keys(payload, {"entity_id", "temperature"})
            temperature = payload.get("temperature")
            if (
                isinstance(temperature, bool)
                or not isinstance(temperature, (int, float))
                or not math.isfinite(temperature)
            ):
                raise ToolInputError(
                    "service_data.temperature must be a finite number",
                    code="invalid_arguments",
                )
            if not (
                self.settings.climate_temperature_min
                <= temperature
                <= self.settings.climate_temperature_max
            ):
                raise ToolInputError(
                    "service_data.temperature must be between "
                    f"{self.settings.climate_temperature_min:g} and "
                    f"{self.settings.climate_temperature_max:g}",
                    code="invalid_arguments",
                )
        elif service_name == "light.turn_on":
            self._only_service_data_keys(payload, {"entity_id", "brightness_pct"})
            if "brightness_pct" in payload:
                brightness = payload["brightness_pct"]
                if (
                    isinstance(brightness, bool)
                    or not isinstance(brightness, int)
                    or not 1 <= brightness <= 100
                ):
                    raise ToolInputError(
                        "service_data.brightness_pct must be an integer between 1 and 100",
                        code="invalid_arguments",
                    )
        return entity_id

    @staticmethod
    def _state_is_unavailable(state: dict[str, Any]) -> bool:
        return state.get("state") in {"unavailable", "unknown", None}

    @staticmethod
    def _state_matches_target(
        domain: str, service: str, state: dict[str, Any], payload: dict[str, Any]
    ) -> bool:
        current = state.get("state")
        if current in {"unavailable", "unknown", None}:
            return False
        if domain == "light" and service == "turn_on":
            if current != "on":
                return False
            if "brightness_pct" not in payload:
                return True
            attributes = state.get("attributes")
            brightness = attributes.get("brightness") if isinstance(attributes, dict) else None
            expected = round(payload["brightness_pct"] * 255 / 100)
            return (
                isinstance(brightness, int)
                and not isinstance(brightness, bool)
                and abs(brightness - expected) <= 1
            )
        if domain == "light" and service == "turn_off":
            return current == "off"
        if domain == "climate" and service == "turn_on":
            return current not in {"off", "unavailable", "unknown", None}
        if domain == "climate" and service == "turn_off":
            return current == "off"
        if domain == "climate" and service == "set_temperature":
            attributes = state.get("attributes")
            current_temperature = (
                attributes.get("temperature") if isinstance(attributes, dict) else None
            )
            target_temperature = payload.get("temperature")
            return (
                isinstance(current_temperature, (int, float))
                and not isinstance(current_temperature, bool)
                and isinstance(target_temperature, (int, float))
                and not isinstance(target_temperature, bool)
                and math.isclose(current_temperature, target_temperature, abs_tol=0.1)
            )
        return False

    async def _read_control_state(
        self, entity_id: str, *, timeout_seconds: float
    ) -> dict[str, Any]:
        return await asyncio.wait_for(
            self.ha.state(
                entity_id,
                timeout_seconds=timeout_seconds,
                retry_transport=False,
            ),
            timeout=timeout_seconds,
        )

    async def _confirm_control_state(
        self,
        *,
        domain: str,
        service: str,
        entity_id: str,
        payload: dict[str, Any],
        deadline: float,
    ) -> dict[str, Any]:
        last_state: dict[str, Any] | None = None
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                last_state = await self._read_control_state(
                    entity_id,
                    timeout_seconds=min(self.settings.control_read_timeout_seconds, remaining),
                )
                if self._state_matches_target(domain, service, last_state, payload):
                    return last_state
            except TimeoutError:
                pass
            except HomeAssistantError as exc:
                if exc.code != "ha_unreachable":
                    raise
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(self.settings.control_settle_seconds, remaining))
        readback = last_state.get("state") if last_state else None
        raise HomeAssistantError(
            _ACTION_UNCONFIRMED_MESSAGE
            + (f" (last state: {readback})" if readback is not None else ""),
            code="action_unconfirmed",
        )

    async def _execute_control(
        self,
        *,
        domain: str,
        service: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        request_id = current_request_id()
        full_service = f"{domain}.{service}"
        deadline = asyncio.get_running_loop().time() + self.settings.control_total_timeout_seconds
        precheck_state: dict[str, Any] | None = None
        precheck_error: str | None = None
        action = "precheck"
        readback_state: Any = None
        http_status: Any = None
        write_outcome: str | None = None
        confirmed = False
        error_code = "-"

        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise HomeAssistantError(
                    _ACTION_UNCONFIRMED_MESSAGE, code="action_unconfirmed"
                )
            try:
                precheck_state = await self._read_control_state(
                    entity_id,
                    timeout_seconds=min(self.settings.control_read_timeout_seconds, remaining),
                )
            except TimeoutError:
                precheck_error = "timeout"
            except HomeAssistantError as exc:
                if exc.code != "ha_unreachable":
                    raise
                precheck_error = exc.code

            if precheck_state is not None:
                if self._state_is_unavailable(precheck_state):
                    raise HomeAssistantError(
                        "Home Assistant target is currently unavailable",
                        code="ha_entity_unavailable",
                    )
                if self._state_matches_target(domain, service, precheck_state, payload):
                    action = "already_satisfied"
                    readback_state = precheck_state.get("state")
                    write_outcome = "skipped"
                    confirmed = True
                    return {
                        "service": full_service,
                        "entity_id": entity_id,
                        "confirmed": True,
                        "already_satisfied": True,
                        "write_attempted": False,
                        "write_outcome": write_outcome,
                        "http_status": None,
                        "readback_state": readback_state,
                    }

            action = "write"
            remaining = deadline - asyncio.get_running_loop().time()
            write_budget = min(self.settings.control_write_timeout_seconds, remaining)
            if write_budget <= 0:
                raise HomeAssistantError(
                    _ACTION_UNCONFIRMED_MESSAGE, code="action_unconfirmed"
                )
            action_result: dict[str, Any] = {}
            write_error_code: str | None = None
            try:
                action_result = await asyncio.wait_for(
                    self.ha.call_service(
                        domain, service, payload, timeout_seconds=write_budget
                    ),
                    timeout=write_budget,
                )
            except TimeoutError:
                write_error_code = "timeout"
            except HomeAssistantError as exc:
                if exc.code == "ha_unreachable":
                    write_error_code = exc.code
                else:
                    raise
            http_status = action_result.get("http_status")
            write_outcome = "transport_ambiguous" if write_error_code else "accepted"

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0 and self.settings.control_settle_seconds > 0:
                await asyncio.sleep(min(self.settings.control_settle_seconds, remaining))
            readback = await self._confirm_control_state(
                domain=domain,
                service=service,
                entity_id=entity_id,
                payload=payload,
                deadline=deadline,
            )
            readback_state = readback.get("state")

            result = dict(action_result)
            result.update(
                {
                    "service": full_service,
                    "entity_id": entity_id,
                    "confirmed": True,
                    "already_satisfied": False,
                    "write_attempted": True,
                    "write_outcome": write_outcome,
                    "http_status": http_status,
                    "readback_state": readback_state,
                }
            )
            confirmed = True
            return result
        except HomeAssistantError as exc:
            error_code = exc.code
            raise
        finally:
            logger.info(
                "%s",
                json.dumps(
                    {
                        "event": "control_completed",
                        "request_id": request_id,
                        "domain": domain,
                        "service": service,
                        "entity_id": entity_id,
                        "action": action,
                        "confirmed": confirmed,
                        "precheck_state": precheck_state.get("state")
                        if precheck_state
                        else None,
                        "precheck_error": precheck_error,
                        "readback_state": readback_state,
                        "ha_http_status": http_status,
                        "write_outcome": write_outcome,
                        "error_code": None if error_code == "-" else error_code,
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ),
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
        entity_id = self._validate_service_payload(domain, service, payload)
        dry_run = arguments.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise ToolInputError("dry_run must be a boolean", code="invalid_arguments")
        if dry_run:
            return {
                "dry_run": True,
                "allowed": True,
                "service": f"{domain}.{service}",
                "entity_id": entity_id,
                "service_data_keys": sorted(payload),
            }
        return await self._execute_control(
            domain=domain,
            service=service,
            entity_id=entity_id,
            payload=payload,
        )


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
