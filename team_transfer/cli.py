"""``assistant_cli team export | inspect | review | import | import-credentials | finish-import``.

The passphrase is typed at a prompt, or read from a file with
``--passphrase-file`` (for example a Docker secret). It is never taken from
the command line, where other processes could read it.
"""

from __future__ import annotations

import argparse
import getpass
import asyncio
from collections import Counter
from pathlib import Path
from typing import Any

from team_transfer.container import TransferFileError, TransferReader
from team_transfer.crypto import TransferKeyError
from team_transfer.format import TransferManifestV1
from team_transfer.sections import RecordOutcome
from team_transfer.service import (
    ImportPlan,
    TransferError,
    default_sections,
    export_source,
    export_team,
    import_team,
    require_active_owner,
)

_FAILURES = (TransferError, TransferFileError, TransferKeyError, ValueError, OSError)


def add_team_parsers(subparsers: Any) -> None:
    team = subparsers.add_parser(
        "team",
        help="Export a team, or review and import selected records",
        description=(
            "Move a team to a fresh instance of a newer version: export from the running old instance, stop it, "
            "then review and import into an empty instance root of the new version. Import more selected records "
            "from the same backup in later batches. Every record the new version cannot "
            "accept is listed with the reason."
        ),
    )
    commands = team.add_subparsers(dest="team_command", required=True)

    export = commands.add_parser(
        "export", help="Write the team's configuration to a new transfer file"
    )
    export.add_argument(
        "--config", required=True, help="The running instance's instance_config.yaml"
    )
    export.add_argument(
        "--owner",
        required=True,
        metavar="PERSON_ID",
        help="An active owner of the team, recorded as the exporter",
    )
    export.add_argument(
        "--out", required=True, help="New transfer file to write (never overwritten)"
    )
    export.add_argument(
        "--skip-credentials",
        action="store_true",
        help="Leave the broker's stored credentials out (when the broker cannot run); people sign in again",
    )
    _passphrase_argument(export)
    export.add_argument("--env", default=None, help="Optional .env file to load first")

    inspect = commands.add_parser(
        "inspect", help="Check a transfer file and show what it holds"
    )
    inspect.add_argument(
        "--in", dest="input_path", required=True, help="The transfer file"
    )
    _passphrase_argument(inspect)

    review = commands.add_parser(
        "review", help="Open the browser to select and edit records before importing"
    )
    review.add_argument(
        "--in", dest="input_path", required=True, help="The transfer file"
    )
    review.add_argument(
        "--instance-root",
        required=True,
        help="Fresh target or a target that already imported from this backup",
    )
    review.add_argument(
        "--review-dir",
        required=True,
        help="Private folder for selections and editable schedule Markdown",
    )
    review.add_argument(
        "--env", default=None, help="Optional .env file with the new database settings"
    )
    _passphrase_argument(review)

    load = commands.add_parser(
        "import",
        help="Import a reviewed batch into a fresh or previously imported instance",
    )
    load.add_argument(
        "--in", dest="input_path", required=True, help="The transfer file"
    )
    load.add_argument(
        "--instance-root",
        required=True,
        help="Fresh target or a target that already imported from this backup",
    )
    load.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing anything",
    )
    load.add_argument(
        "--review",
        default=None,
        help="Folder saved by team review; required when writing",
    )
    load.add_argument(
        "--env", default=None, help="Optional .env file with the new database settings"
    )
    _passphrase_argument(load)

    credentials = commands.add_parser(
        "import-credentials",
        help="After the new instance's first start, store the file's credentials through its broker",
    )
    credentials.add_argument(
        "--in", dest="input_path", required=True, help="The transfer file"
    )
    credentials.add_argument(
        "--config", required=True, help="The new instance's instance_config.yaml"
    )
    credentials.add_argument(
        "--review",
        required=True,
        help="Folder saved by team review with selected credentials",
    )
    _passphrase_argument(credentials)

    finish = commands.add_parser(
        "finish-import",
        help="Store the credentials onboarding's Import left for the first start (the launcher runs this)",
    )
    finish.add_argument(
        "--config", required=True, help="The new instance's instance_config.yaml"
    )


def _passphrase_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--passphrase-file",
        default=None,
        metavar="PATH",
        help="File holding the transfer passphrase (default: ask at a prompt)",
    )


def read_passphrase(path: str | None, *, confirm: bool) -> str:
    if path:
        return Path(path).expanduser().read_text(encoding="utf-8").rstrip("\r\n")
    first = getpass.getpass("Transfer passphrase: ")
    if confirm and getpass.getpass("Type it again: ") != first:
        raise SystemExit("The two passphrases differ. Nothing was written.")
    return first


def run_team_command(args: argparse.Namespace) -> None:
    try:
        if args.team_command == "export":
            _export(args)
        elif args.team_command == "inspect":
            _inspect(args)
        elif args.team_command == "import-credentials":
            _import_credentials(args)
        elif args.team_command == "finish-import":
            _finish_import(args)
        elif args.team_command == "review":
            _review(args)
        else:
            _import(args)
    except _FAILURES as exc:
        raise SystemExit(str(exc)) from exc


def _export(args: argparse.Namespace) -> None:
    source = export_source(args.config)
    require_active_owner(source.config, source.team_id, args.owner)
    passphrase = read_passphrase(args.passphrase_file, confirm=True)
    sections = [
        section
        for section in default_sections()
        if not (args.skip_credentials and section.after_start)
    ]
    report = export_team(
        source, args.out, passphrase, owner_person_id=args.owner, sections=sections
    )
    lines = [f"Wrote {report.path} (mode 0600).", *manifest_summary(report.manifest)]
    lines.append(
        "Stop this instance now: chat bot tokens and model-provider sign-ins must not be used by two running "
        "instances. Keep the file and its passphrase private; it holds the team's configuration."
    )
    print("\n".join(lines))


def _inspect(args: argparse.Namespace) -> None:
    passphrase = read_passphrase(args.passphrase_file, confirm=False)
    with TransferReader.open(args.input_path, passphrase) as reader:
        print(
            "\n".join(
                [
                    "The file is intact and the passphrase matches.",
                    *manifest_summary(reader.manifest),
                ]
            )
        )


def _import(args: argparse.Namespace) -> None:
    passphrase = read_passphrase(args.passphrase_file, confirm=False)
    with TransferReader.open(args.input_path, passphrase) as reader:
        arguments = {}
        if args.review:
            from team_transfer.review import ReviewFiles

            files = ReviewFiles.open(
                reader, args.review, target_root=args.instance_root
            ).snapshot()
            choices, overrides = files.choices(require_confirmation=not args.dry_run)
            arguments = dict(
                choices=choices,
                overrides=overrides,
                allow_existing=True,
                require_valid=True,
            )
        elif not args.dry_run:
            raise TransferError(
                "Open `assistant_cli team review` first, then import with --review pointing to its folder."
            )
        plan = import_team(
            reader, args.instance_root, dry_run=bool(args.dry_run), **arguments
        )
    print("\n".join(plan_summary(plan, dry_run=bool(args.dry_run))))
    if plan.by_outcome(RecordOutcome.REFUSED):
        raise SystemExit(1)


def _import_credentials(args: argparse.Namespace) -> None:
    from team_transfer.broker_credentials import import_credentials

    passphrase = read_passphrase(args.passphrase_file, confirm=False)
    with TransferReader.open(args.input_path, passphrase) as reader:
        from assistant_gateway.config import GatewayInstanceConfig
        from team_transfer.review import ReviewFiles

        config = GatewayInstanceConfig.load_saved(args.config)
        files = ReviewFiles.open(reader, args.review, target_root=config.layout.root)
        choices, _overrides = files.choices(require_confirmation=False)
        report = import_credentials(
            reader,
            args.config,
            selected_keys=sorted(choices.get("broker_credentials", ())),
        )
    stored = [outcome for outcome in report.outcomes if outcome.get("imported")]
    refused = [outcome for outcome in report.outcomes if not outcome.get("imported")]
    lines = [
        f"Stored {len(stored)} of {len(report.outcomes)} credential record(s) through the broker."
    ]
    if refused:
        lines.append("Not stored:")
        lines.extend(
            f"- {outcome['key']}: {outcome.get('reason', '')}" for outcome in refused
        )
    if report.notified:
        lines.append(
            "Told in their Inbox that their sign-ins and keys moved: "
            + ", ".join(report.notified)
        )
    lines.extend(
        f"- {outcome['key']}: {outcome['reason']}"
        for outcome in stored
        if outcome.get("reason")
    )
    print("\n".join(lines))
    if refused:
        raise SystemExit(1)


def _finish_import(args: argparse.Namespace) -> None:
    from team_transfer.credential_handover import import_handed_over_credentials

    report = import_handed_over_credentials(args.config)
    if report is None:
        print("No credentials are waiting from an onboarding import.")
        return
    stored = sum(1 for outcome in report.outcomes if outcome.get("imported"))
    print(
        f"Stored {stored} of {len(report.outcomes)} credential record(s) onboarding's import left."
    )
    # Retryable failures raise TransferError and retain the handover. Terminal
    # refusals are already reported to the owner; the completed step may start
    # the team, including when a newer destination credential was kept.


def _review(args: argparse.Namespace) -> None:
    from team_transfer.review import prepare_review
    from team_transfer.review_web import serve_review

    passphrase = read_passphrase(args.passphrase_file, confirm=False)
    with TransferReader.open(args.input_path, passphrase) as reader:
        files = prepare_review(reader, args.instance_root, args.review_dir)
        try:
            asyncio.run(serve_review(files))
        except KeyboardInterrupt:
            pass


def manifest_summary(manifest: TransferManifestV1) -> list[str]:
    lines = [
        f"Team {manifest.team_id} from instance {manifest.instance_name}, exported "
        f"{manifest.exported_at_utc} by version {manifest.application_version} (owner {manifest.exported_by.owner_person_id}).",
    ]
    if not manifest.sections:
        lines.append("It holds no sections.")
    for entry in manifest.sections:
        kind = " (encrypted)" if entry.encrypted else ""
        lines.append(
            f"- {entry.name} v{entry.schema_version}: {entry.record_count} record(s){kind}"
        )
    return lines


def plan_summary(plan: ImportPlan, *, dry_run: bool) -> list[str]:
    counts = Counter(record.outcome for record in plan.records)
    verb = "Would import" if dry_run else "Imported"
    lines = [
        f"{verb} {counts[RecordOutcome.IMPORT] + counts[RecordOutcome.ATTENTION]} of {len(plan.records)} record(s) "
        f"into {plan.target_root}; {counts[RecordOutcome.REFUSED]} refused.",
    ]
    for title, outcome in (
        ("Needs your attention:", RecordOutcome.ATTENTION),
        ("Refused:", RecordOutcome.REFUSED),
    ):
        listed = plan.by_outcome(outcome)
        if listed:
            lines.append(title)
            lines.extend(
                f"- {record.section} {record.key}: {record.reason}" for record in listed
            )
    if plan.unknown_sections:
        lines.append(
            "Sections this version does not know (not imported): "
            + ", ".join(plan.unknown_sections)
        )
    if plan.missing_sections:
        lines.append(
            "Sections the file does not have: " + ", ".join(plan.missing_sections)
        )
    if plan.later_sections:
        lines.append(
            "Imported after the first start with `assistant_cli team import-credentials`: "
            + ", ".join(plan.later_sections)
        )
    if dry_run:
        lines.append("Nothing was written (dry run).")
    if plan.skipped_records:
        lines.append(
            f"Skipped {len(plan.skipped_records)} unselected or already imported records."
        )
    return lines


__all__ = [
    "add_team_parsers",
    "manifest_summary",
    "plan_summary",
    "read_passphrase",
    "run_team_command",
]
