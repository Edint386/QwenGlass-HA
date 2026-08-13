from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

import httpx
from starlette.testclient import TestClient

from qwen_ha_bridge.app import create_app

LIGHT_ENTITIES = frozenset({"light.living_room"})


def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer bridge-secret", "Content-Type": "application/json"}


def rpc(method: str, *, msg_id: int | None = None, params: dict | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return body


def initialize(client: TestClient) -> tuple[str, dict]:
    response = client.post(
        "/mcp",
        headers=auth_headers(),
        json=rpc(
            "initialize",
            msg_id=1,
            params={
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        ),
    )
    return response.headers["mcp-session-id"], response.json()


def test_authentication_fails_closed_when_key_missing(settings, fake_ha, tmp_path: Path) -> None:
    app = create_app(
        settings.__class__(
            ha_base_url=settings.ha_base_url,
            ha_token_file=settings.ha_token_file,
            bridge_api_key_file=tmp_path / "missing",
            auth_mode="required",
            allowed_hosts=settings.allowed_hosts,
            keepalive_interval_seconds=0,
        ),
        ha_transport=httpx.MockTransport(fake_ha),
    )
    with TestClient(app) as client:
        response = client.post("/mcp", headers=auth_headers(), json=rpc("ping", msg_id=1))
    assert response.status_code == 503


def test_wrong_key_is_unauthorized(settings, fake_ha) -> None:
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers={"Authorization": "Bearer wrong"},
            json=rpc("ping", msg_id=1),
        )
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_initialize_returns_closed_json_and_secure_session(settings, fake_ha) -> None:
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "initialize",
                msg_id=1,
                params={"protocolVersion": "2025-03-26", "capabilities": {}},
            ),
        )
        session_id = response.headers["mcp-session-id"]
        body = response.json()
    assert len(session_id) >= 32
    assert response.headers["content-type"].startswith("application/json")
    assert int(response.headers["content-length"]) == len(response.content)
    assert "transfer-encoding" not in response.headers
    assert body["result"]["protocolVersion"] == "2025-03-26"
    assert body["result"]["capabilities"]["tools"]["listChanged"] is False


def test_qwen_relaxed_flow_does_not_require_initialized_notification(settings, fake_ha) -> None:
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=2,
                params={"name": "bridge_status", "arguments": {}},
            ),
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["authentication_valid"] is True


def test_strict_mode_rejects_missing_session(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(relaxed_sessions=False), ha_transport=httpx.MockTransport(fake_ha)
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp", headers=auth_headers(), json=rpc("tools/list", msg_id=2, params={})
        )
    assert response.status_code == 404


def test_strict_mode_requires_initialized_notification(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(relaxed_sessions=False), ha_transport=httpx.MockTransport(fake_ha)
    )
    with TestClient(app) as client:
        session_id, _ = initialize(client)
        headers = auth_headers() | {"Mcp-Session-Id": session_id}
        before = client.post("/mcp", headers=headers, json=rpc("tools/list", msg_id=2, params={}))
        initialized = client.post("/mcp", headers=headers, json=rpc("notifications/initialized"))
        after = client.post("/mcp", headers=headers, json=rpc("tools/list", msg_id=3, params={}))
    assert before.status_code == 400
    assert initialized.status_code == 202
    assert after.status_code == 200


def test_read_only_catalog_hides_write_tool(settings, fake_ha) -> None:
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.post(
            "/mcp", headers=auth_headers(), json=rpc("tools/list", msg_id=2, params={})
        )
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert names == {"bridge_status", "list_ha_states", "get_ha_state"}


def test_list_states_filters_and_paginates(settings, fake_ha) -> None:
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "list_ha_states",
                    "arguments": {"domain": "light", "limit": 1, "offset": 1},
                },
            ),
        )
    result = response.json()["result"]["structuredContent"]
    assert result["total"] == 2
    assert result["count"] == 1
    assert result["states"][0]["entity_id"] == "light.bedroom"


def test_entity_path_injection_is_rejected_before_ha(settings, fake_ha) -> None:
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "get_ha_state",
                    "arguments": {"entity_id": "../api/services/light/turn_on"},
                },
            ),
        )
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"
    assert not any("services" in path for _, path in fake_ha.calls)


def test_write_tool_allowlist_and_dry_run(make_settings, fake_ha) -> None:
    settings = make_settings(
        enable_write_tools=True,
        allowed_ha_services=frozenset({"light.turn_on"}),
        allowed_ha_entities=LIGHT_ENTITIES,
    )
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        catalog = client.post(
            "/mcp", headers=auth_headers(), json=rpc("tools/list", msg_id=2, params={})
        ).json()["result"]["tools"]
        assert "call_ha_service" in {tool["name"] for tool in catalog}
        dry_run = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {"entity_id": "light.living_room"},
                        "dry_run": True,
                    },
                },
            ),
        ).json()["result"]
    assert dry_run["isError"] is False
    assert dry_run["structuredContent"]["dry_run"] is True
    assert dry_run["structuredContent"]["entity_id"] == "light.living_room"
    assert fake_ha.service_calls == 0


def test_write_tool_catalog_exposes_aliases_and_exact_entities(make_settings, fake_ha) -> None:
    settings = make_settings(
        enable_write_tools=True,
        allowed_ha_services=frozenset({"light.turn_on", "light.turn_off"}),
        allowed_ha_entities=LIGHT_ENTITIES,
        ha_device_aliases=(("客厅灯", "light.living_room"),),
    )
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        catalog = client.post(
            "/mcp", headers=auth_headers(), json=rpc("tools/list", msg_id=2, params={})
        ).json()["result"]["tools"]

    tool = next(item for item in catalog if item["name"] == "call_ha_service")
    assert "Configured aliases are listed once" in tool["description"]
    assert "ask which one instead of guessing" in tool["description"]
    assert "only the unqualified service name" in tool["description"]
    assert "never put `climate.set_temperature` in `service`" in tool["description"]
    assert tool["inputSchema"]["properties"]["domain"]["enum"] == ["light"]
    service_schema = tool["inputSchema"]["properties"]["service"]
    assert service_schema["enum"] == ["turn_off", "turn_on"]
    assert "Do not include the domain or a dot" in service_schema["description"]
    service_data = tool["inputSchema"]["properties"]["service_data"]
    assert service_data["properties"]["entity_id"]["enum"] == ["light.living_room"]
    assert "客厅灯 -> light.living_room" in service_data["properties"]["entity_id"]["description"]


def test_write_tool_executes_one_allowlisted_call(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
            allowed_ha_entities=LIGHT_ENTITIES,
        ),
        ha_transport=httpx.MockTransport(fake_ha),
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {"entity_id": "light.living_room"},
                    },
                },
            ),
        )
    assert response.json()["result"]["isError"] is False
    result = response.json()["result"]["structuredContent"]
    assert result["confirmed"] is True
    assert result["already_satisfied"] is True
    assert result["write_attempted"] is False
    assert result["write_outcome"] == "skipped"
    assert fake_ha.service_calls == 0


def test_write_tool_changes_off_light_and_confirms_readback(make_settings, fake_ha) -> None:
    settings = make_settings(
        enable_write_tools=True,
        allowed_ha_services=frozenset({"light.turn_on"}),
        allowed_ha_entities=LIGHT_ENTITIES,
    )
    fake_ha.states[0]["state"] = "off"
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {
                            "entity_id": "light.living_room",
                            "brightness_pct": 50,
                        },
                    },
                },
            ),
        )
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["confirmed"] is True
    assert result["structuredContent"]["readback_state"] == "on"
    assert fake_ha.service_calls == 1


def test_already_on_climate_skips_write(make_settings) -> None:
    service_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal service_calls
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if request.url.path == "/api/states/climate.living_room":
            return httpx.Response(
                200,
                json={
                    "entity_id": "climate.living_room",
                    "state": "heat",
                    "attributes": {"temperature": 24},
                },
            )
        if request.url.path == "/api/services/climate/turn_on":
            service_calls += 1
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "not found"})

    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"climate.turn_on"}),
            allowed_ha_entities=frozenset({"climate.living_room"}),
        ),
        ha_transport=httpx.MockTransport(handler),
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "climate",
                        "service": "turn_on",
                        "service_data": {"entity_id": "climate.living_room"},
                    },
                },
            ),
        )
    result = response.json()["result"]["structuredContent"]
    assert result["confirmed"] is True
    assert result["already_satisfied"] is True
    assert result["readback_state"] == "heat"
    assert service_calls == 0


def test_write_timeout_can_still_be_confirmed_by_readback(make_settings, fake_ha) -> None:
    fake_ha.states[0]["state"] = "off"
    service_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal service_calls
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if request.url.path == "/api/states/light.living_room":
            return httpx.Response(200, json=fake_ha.states[0])
        if request.url.path == "/api/services/light/turn_on":
            service_calls += 1
            fake_ha.states[0]["state"] = "on"
            await asyncio.sleep(0.08)
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "not found"})

    settings = make_settings(
        enable_write_tools=True,
        allowed_ha_services=frozenset({"light.turn_on"}),
        allowed_ha_entities=LIGHT_ENTITIES,
        control_total_timeout_seconds=0.3,
        control_read_timeout_seconds=0.05,
        control_write_timeout_seconds=0.05,
        control_settle_seconds=0.01,
    )
    app = create_app(settings, ha_transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {"entity_id": "light.living_room"},
                    },
                },
            ),
        )
    result = response.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["confirmed"] is True
    assert result["structuredContent"]["write_outcome"] == "transport_ambiguous"
    assert service_calls == 1


def test_unconfirmed_write_returns_explicit_uncertainty(make_settings, fake_ha) -> None:
    fake_ha.states[0]["state"] = "off"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if request.url.path == "/api/states/light.living_room":
            return httpx.Response(200, json=fake_ha.states[0])
        if request.url.path == "/api/services/light/turn_on":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": "not found"})

    settings = make_settings(
        enable_write_tools=True,
        allowed_ha_services=frozenset({"light.turn_on"}),
        allowed_ha_entities=LIGHT_ENTITIES,
        control_total_timeout_seconds=0.12,
        control_read_timeout_seconds=0.02,
        control_write_timeout_seconds=0.05,
        control_settle_seconds=0.01,
    )
    app = create_app(settings, ha_transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {"entity_id": "light.living_room"},
                    },
                },
            ),
        )
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "action_unconfirmed"
    assert "Do not report success or failure" in result["structuredContent"]["error"]


def test_control_logs_correlate_without_arguments_or_secrets(
    make_settings, fake_ha, caplog
) -> None:
    fake_ha.states[0]["state"] = "off"
    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
            allowed_ha_entities=LIGHT_ENTITIES,
        ),
        ha_transport=httpx.MockTransport(fake_ha),
    )
    caplog.set_level(logging.INFO)
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {
                            "entity_id": "light.living_room",
                            "brightness_pct": 37,
                        },
                    },
                },
            ),
        )
    events = []
    for record in caplog.records:
        with contextlib.suppress(json.JSONDecodeError):
            events.append(json.loads(record.getMessage()))
    by_name = {event.get("event"): event for event in events if event.get("event")}
    request_id = response.headers["x-request-id"]
    assert by_name["request_completed"]["request_id"] == request_id
    assert by_name["tool_completed"]["request_id"] == request_id
    assert by_name["control_completed"]["request_id"] == request_id
    assert by_name["tool_completed"]["is_error"] is False
    assert by_name["control_completed"]["confirmed"] is True
    assert by_name["control_completed"]["write_outcome"] == "accepted"
    combined = "\n".join(record.getMessage() for record in caplog.records)
    assert "brightness_pct" not in combined
    assert "bridge-secret" not in combined
    assert "ha-secret" not in combined


def test_unavailable_entity_is_rejected_before_write(make_settings, fake_ha) -> None:
    fake_ha.states[0]["state"] = "unavailable"
    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
            allowed_ha_entities=LIGHT_ENTITIES,
        ),
        ha_transport=httpx.MockTransport(fake_ha),
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {"entity_id": "light.living_room"},
                    },
                },
            ),
        )
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "ha_entity_unavailable"
    assert fake_ha.service_calls == 0


def test_unlisted_service_is_blocked(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
            allowed_ha_entities=LIGHT_ENTITIES,
        ),
        ha_transport=httpx.MockTransport(fake_ha),
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {"domain": "lock", "service": "unlock"},
                },
            ),
        )
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "service_not_allowed"
    assert fake_ha.service_calls == 0


def test_write_tool_requires_exact_entity(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
            allowed_ha_entities=LIGHT_ENTITIES,
        ),
        ha_transport=httpx.MockTransport(fake_ha),
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {},
                        "dry_run": True,
                    },
                },
            ),
        )
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "invalid_arguments"
    assert fake_ha.service_calls == 0


def test_write_tool_blocks_unlisted_entity(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
            allowed_ha_entities=LIGHT_ENTITIES,
        ),
        ha_transport=httpx.MockTransport(fake_ha),
    )
    with TestClient(app) as client:
        response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {"entity_id": "light.bedroom"},
                        "dry_run": True,
                    },
                },
            ),
        )
    result = response.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["code"] == "entity_not_allowed"
    assert fake_ha.service_calls == 0


def test_write_tool_blocks_alternate_target_and_domain_mismatch(make_settings, fake_ha) -> None:
    settings = make_settings(
        enable_write_tools=True,
        allowed_ha_services=frozenset({"light.turn_on", "climate.turn_on"}),
        allowed_ha_entities=frozenset({"light.living_room", "climate.living_room"}),
    )
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        area_response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {
                            "entity_id": "light.living_room",
                            "area_id": "living_room",
                        },
                        "dry_run": True,
                    },
                },
            ),
        ).json()["result"]
        mismatch_response = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=4,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {"entity_id": "climate.living_room"},
                        "dry_run": True,
                    },
                },
            ),
        ).json()["result"]

    assert area_response["structuredContent"]["code"] == "invalid_arguments"
    assert mismatch_response["structuredContent"]["code"] == "invalid_arguments"
    assert fake_ha.service_calls == 0


def test_climate_temperature_and_light_brightness_are_bounded(make_settings, fake_ha) -> None:
    settings = make_settings(
        enable_write_tools=True,
        allowed_ha_services=frozenset({"climate.set_temperature", "light.turn_on"}),
        allowed_ha_entities=frozenset({"climate.living_room", "light.living_room"}),
        climate_temperature_min=16,
        climate_temperature_max=30,
    )
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        too_hot = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=3,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "climate",
                        "service": "set_temperature",
                        "service_data": {
                            "entity_id": "climate.living_room",
                            "temperature": 31,
                        },
                        "dry_run": True,
                    },
                },
            ),
        ).json()["result"]
        invalid_brightness = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=4,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "light",
                        "service": "turn_on",
                        "service_data": {
                            "entity_id": "light.living_room",
                            "brightness_pct": 101,
                        },
                        "dry_run": True,
                    },
                },
            ),
        ).json()["result"]
        valid_temperature = client.post(
            "/mcp",
            headers=auth_headers(),
            json=rpc(
                "tools/call",
                msg_id=5,
                params={
                    "name": "call_ha_service",
                    "arguments": {
                        "domain": "climate",
                        "service": "set_temperature",
                        "service_data": {
                            "entity_id": "climate.living_room",
                            "temperature": 24,
                        },
                        "dry_run": True,
                    },
                },
            ),
        ).json()["result"]

    assert too_hot["structuredContent"]["code"] == "invalid_arguments"
    assert invalid_brightness["structuredContent"]["code"] == "invalid_arguments"
    assert valid_temperature["isError"] is False
    assert fake_ha.service_calls == 0


def test_mcp_header_body_mismatch_is_rejected(settings, fake_ha) -> None:
    headers = auth_headers() | {"Mcp-Method": "tools/call"}
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.post("/mcp", headers=headers, json=rpc("tools/list", msg_id=2, params={}))
    assert response.json()["error"]["code"] == -32600


def test_request_body_limit_is_enforced(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(request_body_limit=128), ha_transport=httpx.MockTransport(fake_ha)
    )
    body = json.dumps(rpc("ping", msg_id=1, params={"padding": "x" * 256}))
    with TestClient(app) as client:
        response = client.post("/mcp", headers=auth_headers(), content=body)
    assert response.status_code == 413


def test_delete_terminates_session(settings, fake_ha) -> None:
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        session_id, _ = initialize(client)
        response = client.delete(
            "/mcp",
            headers=auth_headers() | {"Mcp-Session-Id": session_id},
        )
        second = client.delete(
            "/mcp",
            headers=auth_headers() | {"Mcp-Session-Id": session_id},
        )
    assert response.status_code == 204
    assert second.status_code == 404


def test_health_does_not_expose_ha_details(settings, fake_ha) -> None:
    app = create_app(settings, ha_transport=httpx.MockTransport(fake_ha))
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    text = response.text.lower()
    assert "token" not in text
    assert "ha.local" not in text
