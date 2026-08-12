from __future__ import annotations

import json
from pathlib import Path

import httpx
from starlette.testclient import TestClient

from qwen_ha_bridge.app import create_app


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
    assert fake_ha.service_calls == 0


def test_write_tool_executes_one_allowlisted_call(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
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
    assert fake_ha.service_calls == 1


def test_unlisted_service_is_blocked(make_settings, fake_ha) -> None:
    app = create_app(
        make_settings(
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
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
