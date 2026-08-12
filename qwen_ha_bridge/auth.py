"""Bridge-level bearer authentication."""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from .config import Settings
from .errors import CredentialError
from .secrets import SecretFile


class Authenticator:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._key = SecretFile(settings.bridge_api_key_file, label="bridge API key")

    def authorize(self, headers: Mapping[str, str]) -> str | None:
        """Return None on success, otherwise an HTTP-safe error category."""
        if self.settings.auth_mode == "disabled":
            return None
        try:
            expected = self._key.read_required()
        except CredentialError:
            return "unavailable"
        authorization = headers.get("authorization", "")
        supplied = ""
        if authorization.lower().startswith("bearer "):
            supplied = authorization[7:].strip()
        if not supplied:
            supplied = headers.get("x-bridge-token", "").strip()
        if not supplied or not hmac.compare_digest(supplied, expected):
            return "unauthorized"
        return None
