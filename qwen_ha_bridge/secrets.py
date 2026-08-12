"""Live, permission-checked credential loading."""

from __future__ import annotations

import stat
from pathlib import Path

from .errors import CredentialError


class SecretFile:
    """Read a small one-line secret without caching it in process globals."""

    def __init__(self, path: Path, *, label: str, max_bytes: int = 8_192) -> None:
        self.path = path
        self.label = label
        self.max_bytes = max_bytes

    def read_optional(self) -> str | None:
        try:
            info = self.path.stat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise CredentialError(
                f"{self.label} is not readable", code="credential_unavailable"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise CredentialError(
                f"{self.label} must be a regular file", code="unsafe_credential_file"
            )
        if info.st_mode & 0o077:
            raise CredentialError(
                f"{self.label} must not be accessible by group or others",
                code="unsafe_credential_file",
            )
        if info.st_size > self.max_bytes:
            raise CredentialError(f"{self.label} is too large", code="unsafe_credential_file")
        try:
            value = self.path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as exc:
            raise CredentialError(
                f"{self.label} is not readable", code="credential_unavailable"
            ) from exc
        if len(value.encode("utf-8")) > self.max_bytes:
            raise CredentialError(f"{self.label} is too large", code="unsafe_credential_file")
        if "\n" in value or "\r" in value:
            raise CredentialError(
                f"{self.label} must contain one line", code="unsafe_credential_file"
            )
        return value or None

    def read_required(self) -> str:
        value = self.read_optional()
        if value is None:
            raise CredentialError(f"{self.label} is not configured", code="credential_unavailable")
        return value
