from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwen_ha_bridge.config import Settings
from qwen_ha_bridge.errors import ConfigurationError, CredentialError
from qwen_ha_bridge.secrets import SecretFile


def test_write_tools_require_authentication(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="require BRIDGE_AUTH_MODE"):
        Settings(
            ha_token_file=settings.ha_token_file,
            bridge_api_key_file=settings.bridge_api_key_file,
            auth_mode="disabled",
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
            allowed_ha_entities=frozenset({"light.living_room"}),
        )


def test_write_tools_require_allowlist(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="non-empty ALLOWED_HA_SERVICES"):
        Settings(
            ha_token_file=settings.ha_token_file,
            bridge_api_key_file=settings.bridge_api_key_file,
            auth_mode="required",
            enable_write_tools=True,
        )


def test_write_tools_require_entity_allowlist(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="non-empty ALLOWED_HA_ENTITIES"):
        Settings(
            ha_token_file=settings.ha_token_file,
            bridge_api_key_file=settings.bridge_api_key_file,
            auth_mode="required",
            enable_write_tools=True,
            allowed_ha_services=frozenset({"light.turn_on"}),
        )


def test_alias_targets_must_be_allowlisted(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="targets must be present"):
        Settings(
            ha_token_file=settings.ha_token_file,
            bridge_api_key_file=settings.bridge_api_key_file,
            allowed_ha_entities=frozenset({"light.living_room"}),
            ha_device_aliases=(("卧室灯", "light.bedroom"),),
        )


def test_device_aliases_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_HA_ENTITIES", "climate.living_room,light.desk")
    monkeypatch.setenv(
        "HA_DEVICE_ALIASES_JSON",
        '{"空调":"climate.living_room","挂灯":"light.desk"}',
    )

    loaded = Settings.from_env()

    assert loaded.allowed_ha_entities == frozenset({"climate.living_room", "light.desk"})
    assert loaded.ha_device_aliases == (
        ("空调", "climate.living_room"),
        ("挂灯", "light.desk"),
    )


def test_invalid_device_alias_json_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_DEVICE_ALIASES_JSON", "not-json")

    with pytest.raises(ConfigurationError, match="must be valid JSON"):
        Settings.from_env()


def test_control_phase_timeouts_must_fit_total_budget(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="phase timeouts must fit"):
        Settings(
            ha_token_file=settings.ha_token_file,
            bridge_api_key_file=settings.bridge_api_key_file,
            control_total_timeout_seconds=1.0,
            control_read_timeout_seconds=0.25,
            control_write_timeout_seconds=0.75,
            control_settle_seconds=0.1,
        )


def test_control_total_timeout_must_stay_below_qwen_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HA_CONTROL_TOTAL_TIMEOUT_SECONDS", "2.1")

    with pytest.raises(ConfigurationError, match=r"between 0\.2 and 2\.0"):
        Settings.from_env()

    with pytest.raises(ConfigurationError, match=r"must be at most 2\.0"):
        Settings(control_total_timeout_seconds=2.1)


def test_device_alias_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    aliases = {f"灯{i}": "light.desk" for i in range(65)}
    monkeypatch.setenv("HA_DEVICE_ALIASES_JSON", json.dumps(aliases))

    with pytest.raises(ConfigurationError, match="at most 64 aliases"):
        Settings.from_env()


def test_secret_file_rejects_group_readable(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text("value", encoding="utf-8")
    path.chmod(0o640)
    with pytest.raises(CredentialError, match="group or others"):
        SecretFile(path, label="test secret").read_required()


def test_secret_file_is_live_read(tmp_path: Path) -> None:
    path = tmp_path / "secret"
    path.write_text("first", encoding="utf-8")
    path.chmod(0o600)
    secret = SecretFile(path, label="test secret")
    assert secret.read_required() == "first"
    path.write_text("second", encoding="utf-8")
    path.chmod(0o600)
    assert secret.read_required() == "second"
