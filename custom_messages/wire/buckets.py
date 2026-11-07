"""The stream behind a protocol 1 key-value bucket, as the topology owner creates it.

contracts/messages/v1/README.md ("Key-value grants"): every bucket's stream has the
settings of ``transport.yaml`` ``key_value_stream``. Client libraries' bucket helpers
(nats-py's ``create_key_value``) allow rollup headers, and with them a writer of one
key erases every key of the bucket with ``Nats-Rollup: all``; so the topology owner
creates each bucket as a stream with these settings, and clients then bind to it as a
bucket (nats-py ``key_value(bucket)``). A bucket whose name holds ``{team}`` exists once
per team.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

TEAM_PLACEHOLDER = "{team}"


def bucket_name(bucket: str, team: Optional[str] = None) -> str:
    """The bucket's name, filled with ``team`` for a bucket per team."""

    if TEAM_PLACEHOLDER in bucket:
        if not team:
            raise ValueError(f"{bucket} exists once per team; name the team")
        return bucket.replace(TEAM_PLACEHOLDER, team)
    return bucket


def bucket_stream(transport: Mapping[str, Any], bucket: str, team: Optional[str] = None) -> dict[str, Any]:
    """The stream settings of ``bucket`` (a ``key_value`` name of the generated ``transport.json``).

    The result uses NATS stream configuration names (nats-py ``StreamConfig`` fields);
    ``storage`` stays ``file`` or ``memory`` for the caller to map. A bucket with a
    per-key lifetime keeps each value for that long (``max_age``).
    """

    spec = transport["key_value"][bucket]
    settings = transport["key_value_stream"]
    name = bucket_name(bucket, team)
    config: dict[str, Any] = {
        "name": f"KV_{name}",
        "subjects": [f"$KV.{name}.>"],
        "max_msgs_per_subject": settings["history"],
        "allow_rollup_hdrs": settings["allow_rollup_hdrs"],
        "deny_delete": settings["deny_delete"],
        "deny_purge": settings["deny_purge"],
        "allow_direct": settings["allow_direct"],
        "storage": settings["storage"],
        "max_consumers": settings["max_consumers"],
    }
    if spec["per_key_ttl_seconds"]:
        config["max_age"] = spec["per_key_ttl_seconds"]
    return config
