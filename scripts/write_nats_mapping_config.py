"""Write a nats-server configuration file holding the account's partition mappings, for a live-test server.

Protocol 1 hosts subscribe to an agent's live control address with its partition token, and
the installation authority refuses to start on a server that does not map agents' addresses
(contract README, "Partitions"). The bundled server's configuration carries the mappings at
the installation's count (``docker.nats_service``); a server the live tests share needs them
at the contract's default count:

    python scripts/write_nats_mapping_config.py /path/to/mappings.conf
    nats-server -js -c /path/to/mappings.conf

CI's live NATS job starts its server with this file too.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_messages.wire.kinds import DEFAULT_PARTITION_COUNT  # noqa: E402
from docker.nats_service import mappings_block  # noqa: E402

LOGGER = logging.getLogger("write_nats_mapping_config")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a nats-server configuration file with the partition mappings.")
    parser.add_argument("path", type=Path, help="The configuration file to write (replaced when it exists).")
    parser.add_argument("--partitions", type=int, default=DEFAULT_PARTITION_COUNT,
                        help=f"The partition count to map with (default: the contract's {DEFAULT_PARTITION_COUNT}).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        text = mappings_block(args.partitions)
    except ValueError as error:
        parser.error(str(error))
    args.path.parent.mkdir(parents=True, exist_ok=True)
    args.path.write_text(text, encoding="utf-8")
    LOGGER.info("Wrote the partition mappings. event=nats_mapping_config_written path=%s partitions=%s",
                args.path, args.partitions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
