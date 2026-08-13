---
name: qwen-openapi-timeout-probe
description: Use the isolated read-only OpenAPI delay probe to measure Qwen client timeout and disconnect behavior at fixed delays from 0 to 3000 milliseconds. Use only for diagnostics; the probe does not connect to Home Assistant and cannot inspect or control any device.
---

# OpenAPI Timeout Probe

Use only the `probeDelay` operation from `references/openapi.yaml`. This Skill
measures client behavior over the real HTTPS path; it has no Home Assistant or
device capability.

## Run a measurement

1. Call delays in this order: `0`, `500`, `1000`, `1500`, `2000`, `2500`,
   `3000` milliseconds.
2. Make one call per delay. Do not retry a timed-out call automatically because
   the disconnect itself is the measurement.
3. Record whether a response arrived. For successful calls, retain
   `requested_delay_ms`, `server_elapsed_ms`, and `request_id`.
4. Report the largest delay that returned successfully and the first delay that
   timed out or disconnected. Treat the client budget as lying between those
   two tested values; do not claim millisecond precision from the grid.
5. Include the approximate local test time and successful `request_id` values
   when server-log correlation is requested.

## Guardrails

- Accept only the delay values in the operation schema.
- Never describe the probe result as Home Assistant latency or device status.
- Never claim that the probe controls a device, reads a secret, or validates an
  MCP action.
- Do not publish this diagnostic Skill or API as a general device-control
  integration.
