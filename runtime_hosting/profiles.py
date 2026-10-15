"""Trusted registry for operator-authored execution profiles."""

from __future__ import annotations

from collections.abc import Iterable

from runtime_hosting.execution_contracts import ExecutionProfileV1


class ExecutionProfileNotFound(LookupError):
    pass


class ExecutionProfileRegistry:
    def __init__(self, profiles: Iterable[ExecutionProfileV1]):
        indexed: dict[str, ExecutionProfileV1] = {}
        for profile in profiles:
            if profile.profile_id in indexed:
                raise ValueError(f"Duplicate execution profile id: {profile.profile_id!r}.")
            indexed[profile.profile_id] = profile
        self._profiles = indexed

    def require(self, *, profile_id: str, profile_digest: str) -> ExecutionProfileV1:
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ExecutionProfileNotFound(f"Execution profile {profile_id!r} is not installed.")
        if profile.profile_digest != profile_digest:
            raise ExecutionProfileNotFound(
                f"Execution profile {profile_id!r} changed after the revision was built."
            )
        return profile

    def list_profiles(self) -> tuple[ExecutionProfileV1, ...]:
        return tuple(self._profiles[key] for key in sorted(self._profiles))


__all__ = ["ExecutionProfileNotFound", "ExecutionProfileRegistry"]
