"""The team services' word that they serve, which the launcher waits for (``docker.message_hosts``).

Once an instance holds the team's lock and answers at the team's address, it writes
``runtime/team-services-serving.json`` (``InstanceLayout.team_services_serving_path``),
naming its process. Until then other hosts' requests to the team services get "no
responders", and the gateway answers chats "unavailable"; after an unclean stop that
lasts until the dead instance's lease on the lock runs out. So the launcher starts the
team lead, the startup agents and the gateway only once the file names the process it
started. It removes the file before it starts the team services, and a file naming
another process never counts, so a file left by an earlier process cannot pass for
this one. The file is written whole and holds no secret.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from typing import Optional

from custom_messages.wire.encoding import format_timestamp
from runtime_settings.instance_layout import InstanceLayout


def mark_serving(layout: InstanceLayout, instance: str, now: datetime) -> None:
    """Say that this process serves the team, as lock instance ``instance``, from ``now``."""

    path = layout.team_services_serving_path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    temporary.write_text(json.dumps({"pid": os.getpid(), "instance": instance,
                                     "serving_since": format_timestamp(now)}) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def serving_process(layout: InstanceLayout) -> Optional[int]:
    """The process id the file names; ``None`` while there is no file."""

    path = layout.team_services_serving_path
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    try:
        return int(document["pid"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{path} does not name a process") from error


def forget_serving(layout: InstanceLayout) -> None:
    """Remove the file before the team services start, so only the new process's file counts."""

    layout.team_services_serving_path.unlink(missing_ok=True)
