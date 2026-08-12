"""Public, sanitized error types used across the bridge."""

from __future__ import annotations


class BridgeError(Exception):
    """An expected failure whose message is safe to return to the MCP client."""

    def __init__(self, message: str, *, code: str = "bridge_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class ConfigurationError(BridgeError):
    """Invalid deployment configuration."""


class CredentialError(BridgeError):
    """A credential is missing, unsafe, or unreadable."""


class HomeAssistantError(BridgeError):
    """A sanitized Home Assistant communication failure."""


class ToolInputError(BridgeError):
    """Invalid model-provided tool input."""


class ToolPermissionError(BridgeError):
    """A tool invocation is blocked by policy."""
