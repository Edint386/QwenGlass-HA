---
name: qwen-ha-mcp-control
description: Use the mounted Home Assistant MCP tools for private device queries and explicit control requests, including Chinese commands to turn an allowlisted device on or off, set an allowlisted climate temperature, or set an allowlisted light brightness. Apply the live tool schema's aliases and confirmation semantics; never guess ambiguous devices or retry uncertain writes.
---

# Home Assistant MCP Control

Use the mounted Home Assistant MCP catalog as the only source of device IDs,
aliases, allowed services, and parameter ranges. Do not maintain or invent a
second device inventory in this Skill.

## Route the request

- For a status question, use `get_ha_state` when the entity is uniquely known.
- Use `list_ha_states` only when the spoken device is unknown or ambiguous.
- For an explicit control request with one unique configured alias, call
  `call_ha_service` directly. The bridge performs its own bounded state check.
- If a generic word such as "灯" or "灯带" matches multiple entities, ask the
  user which device they mean. Do not choose one.

## Build control arguments

- Pass the domain and unqualified service separately. Use `domain=climate`
  with `service=turn_on`, never `service=climate.turn_on`.
- Put exactly one entity in `service_data.entity_id`.
- Map "打开/开启" to `turn_on` and "关闭" to `turn_off`.
- Map a climate temperature request to `climate` + `set_temperature` and pass
  `temperature` within the live schema range.
- Map a light brightness percentage to `light` + `turn_on` and pass
  `brightness_pct` within the live schema range.
- Use `dry_run=false` only for an explicit user request to operate a device.
  Never turn a diagnostic, example, or hypothetical request into a real write.

## Interpret the result

- Treat `isError=false` with `confirmed=true` as success.
- If `already_satisfied=true`, say the device was already in the requested
  state; do not claim a new action was necessary.
- If the error code is `action_unconfirmed`, say only that the result is not
  confirmed and the device may already have acted. Do not call it a definite
  success or failure, and never retry the write automatically. A single
  read-only `get_ha_state` check is allowed when the target is known.
- If the error code is `ha_entity_unavailable`, say the device is currently
  unavailable in Home Assistant. Do not retry automatically.
- For any other error, report the concise error without exposing internal
  addresses, credentials, headers, logs, or raw payloads.

Keep replies brief and action-focused. Do not mention tool mechanics unless the
user asks for diagnostics.
