from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from qwen_ha_bridge.config import Settings


def write_secret(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


class FakeHomeAssistant:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.service_calls = 0
        self.states = [
            {
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"friendly_name": "Living room"},
            },
            {
                "entity_id": "sensor.temperature",
                "state": "21.5",
                "attributes": {"friendly_name": "Temperature"},
            },
            {
                "entity_id": "light.bedroom",
                "state": "off",
                "attributes": {"friendly_name": "Bedroom"},
            },
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        if request.headers.get("Authorization") != "Bearer ha-secret":
            return httpx.Response(401, json={"message": "unauthorized"})
        if request.url.path == "/api/":
            return httpx.Response(200, json={"message": "API running."})
        if request.url.path == "/api/states":
            return httpx.Response(200, json=self.states)
        if request.url.path.startswith("/api/states/"):
            entity_id = request.url.path.removeprefix("/api/states/")
            for state in self.states:
                if state["entity_id"] == entity_id:
                    return httpx.Response(200, json=state)
            return httpx.Response(404, json={"message": "not found"})
        if request.url.path == "/api/services/light/turn_on":
            self.service_calls += 1
            return httpx.Response(200, json=[{"context": {"id": "redacted"}}])
        return httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def fake_ha() -> FakeHomeAssistant:
    return FakeHomeAssistant()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        ha_base_url="http://ha.local",
        ha_token_file=write_secret(tmp_path / "ha_token", "ha-secret"),
        bridge_api_key_file=write_secret(tmp_path / "bridge_key", "bridge-secret"),
        auth_mode="required",
        enable_write_tools=False,
        allowed_hosts=("testserver", "localhost", "127.0.0.1"),
        keepalive_interval_seconds=0,
    )


@pytest.fixture
def make_settings(settings: Settings):
    def factory(**changes: Any) -> Settings:
        return replace(settings, **changes)

    return factory
