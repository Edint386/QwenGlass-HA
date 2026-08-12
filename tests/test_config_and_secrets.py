from __future__ import annotations

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
        )


def test_write_tools_require_allowlist(settings: Settings) -> None:
    with pytest.raises(ConfigurationError, match="non-empty ALLOWED_HA_SERVICES"):
        Settings(
            ha_token_file=settings.ha_token_file,
            bridge_api_key_file=settings.bridge_api_key_file,
            auth_mode="required",
            enable_write_tools=True,
        )


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
