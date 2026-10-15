"""Shared auth status models for secure broker providers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class AuthStatus:
    provider: str
    status: str
    authenticated: bool
    message: str
    auth_options: tuple[str, ...] = ()
    details: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "provider": self.provider,
            "status": self.status,
            "authenticated": self.authenticated,
            "message": self.message,
            "auth_options": list(self.auth_options),
        }
        if self.details:
            body["details"] = dict(self.details)
        return body
