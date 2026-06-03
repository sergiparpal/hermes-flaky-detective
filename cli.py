"""CLI subcommands for ``hermes flaky-detective <subcommand>``.

Registered as a single top-level command via ``ctx.register_cli_command`` (see
``__init__.register``). ``setup_parser`` defines the argparse subcommands and
``handle`` dispatches them, returning a process exit code.

Subcommands: ``scan`` (detect + persist + report), ``status`` (resolved config +
paths + last scan), ``list`` (the stored verdicts), and ``install-cron`` (Phase 7
— wire the nightly no-agent job).
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def setup_parser(subparser) -> None:
    """Define ``flaky-detective`` subcommands. Called by Hermes during registration."""
    subs = subparser.add_subparsers(dest="flaky_command", required=True)

    p_scan = subs.add_parser("scan", help="Detect flaky tests, persist verdicts, and report")
    p_scan.add_argument("--window", type=int, default=None,
                        help="Detection window in days (default: from config)")
    p_scan.add_argument("--min-fails", dest="min_fails", type=int, default=None,
                        help="Minimum failures in the window to be non-stable (default: from config)")
    p_scan.add_argument("--include-errors", dest="include_errors",
                        action=argparse.BooleanOptionalAction, default=None,
                        help="Count `error` status as a failure (default: from config)")
    p_scan.add_argument("--format", choices=["human", "cron", "json"], default="human",
                        help="Output format (default: human)")

    subs.add_parser("status", help="Show resolved config, DB paths, and the last scan")

    p_list = subs.add_parser("list", help="List stored verdicts")
    p_list.add_argument("--status", choices=["flaky", "consistently_failing", "all"],
                        default="flaky", help="Which verdicts to list (default: flaky)")

    p_cron = subs.add_parser("install-cron", help="Install the nightly no-agent detection job")
    p_cron.add_argument("--schedule", default=None, help="Cron expression (default: from config)")
    p_cron.add_argument("--deliver", default=None, help="Delivery channel (default: from config)")
    p_cron.add_argument("--window", type=int, default=None, help="Detection window in days")
    p_cron.add_argument("--min-fails", dest="min_fails", type=int, default=None,
                        help="Minimum failures in the window to be non-stable")
    p_cron.add_argument("--no-create", dest="no_create", action="store_true",
                        help="Only write the shim + config; print the command instead of creating the job")


def handle(args) -> int:
    """Dispatch to the selected subcommand. Returns a process exit code."""
    handlers = {
        "scan": _cmd_scan,
        "status": _cmd_status,
        "list": _cmd_list,
        "install-cron": _cmd_install_cron,
    }
    sub = getattr(args, "flaky_command", None)
    fn = handlers.get(sub)
    if fn is None:
        print("error: no subcommand given (try `hermes flaky-detective --help`)")
        return 2
    return fn(args)


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _resolve_scan_params(args, cfg):
    window_days = args.window if args.window is not None else int(cfg["window_days"])
    min_fails = args.min_fails if args.min_fails is not None else int(cfg["min_fails"])
    include_errors = (args.include_errors if args.include_errors is not None
                      else bool(cfg["include_errors"]))
    return window_days, min_fails, include_errors


def _validate_tunables(window_days: int, min_fails: int) -> None:
    """Reject nonsensical tunables before they produce misleading verdicts.

    ``min_fails`` must be ``>= 1``: with ``min_fails <= 0`` the ``fails >= min_fails``
    test is always true, so every all-passing (perfectly stable) test would be
    mislabeled *flaky*. ``window_days`` must be ``>= 1``: a zero/negative window
    selects no runs. Raises ``ValueError`` with a one-line, joined message.
    """
    problems = []
    if window_days < 1:
        problems.append(f"--window must be >= 1 (got {window_days})")
    if min_fails < 1:
        problems.append(f"--min-fails must be >= 1 (got {min_fails})")
    if problems:
        raise ValueError("; ".join(problems))


def _cmd_scan(args) -> int:
    from . import config, detect, domain, query, reporting, storage, timeutil

    cfg = config.get_config()
    window_days, min_fails, include_errors = _resolve_scan_params(args, cfg)
    report_scope = cfg.get("report_scope", domain.DEFAULT_REPORT_SCOPE)
    fmt = args.format

    try:
        _validate_tunables(window_days, min_fails)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    cutoff = timeutil.window_cutoff(now, window_days)
    db_path = config.resolve_test_history_db_path(cfg)

    try:
        read = query.read_test_history(db_path, cutoff)
    except query.TestHistoryUnavailable as exc:
        # No test-history data to scan. For cron, stay silent on stdout (an empty
        # tick) so the nightly job does not alert every night before results are
        # ingested; the note still goes to stderr/logs. For interactive formats,
        # surface the message and a non-zero exit.
        if fmt == "cron":
            print(str(exc), file=sys.stderr)
            return 0
        print(f"error: {exc}", file=sys.stderr)
        return 1

    conn = storage.get_connection()

    if not read.rows:
        # test-history is reachable but has no runs in the window (e.g. every run
        # has aged past it). Do NOT overwrite the snapshot with an empty one: that
        # would make every previously-flaky test report as "no longer flaky" in the
        # changes-only cron and make is_flaky answer "unknown" for everything. Keep
        # the prior verdicts; record an audit row noting nothing was examined.
        on_record = sum(storage.count_by_status(conn).values())
        storage.record_scan_run(
            conn, window_days=window_days, min_fails=min_fails, include_errors=include_errors,
            source_schema_version=read.source_schema_version, tests_examined=0, flaky_found=0,
        )
        if fmt == "json":
            empty_meta = {
                "window_days": window_days, "min_fails": min_fails,
                "include_errors": include_errors,
                "source_schema_version": read.source_schema_version,
                "tests_examined": 0, "flaky_found": 0,
            }
            print(reporting.render_json([], empty_meta, [], []))
        elif fmt != "cron":  # human; cron stays silent (an empty tick)
            print(f"No test runs in the last {window_days} day(s); "
                  f"keeping the previous verdicts ({on_record} on record).")
        return 0

    verdicts = detect.compute_verdicts(read.rows, now, window_days, min_fails, include_errors)
    new_flaky_keys = {v.test_key for v in verdicts if v.status == domain.VERDICT_FLAKY}

    prev_flaky_keys = storage.read_flaky_keys(conn)        # BEFORE replace (for the diff)
    storage.replace_verdicts(conn, verdicts)
    storage.record_scan_run(
        conn, window_days=window_days, min_fails=min_fails, include_errors=include_errors,
        source_schema_version=read.source_schema_version,
        tests_examined=len(verdicts), flaky_found=len(new_flaky_keys),
    )

    newly_flaky, newly_resolved = reporting.flaky_changes(prev_flaky_keys, new_flaky_keys)
    scan_meta = {
        "window_days": window_days, "min_fails": min_fails, "include_errors": include_errors,
        "source_schema_version": read.source_schema_version,
        "tests_examined": len(verdicts), "flaky_found": len(new_flaky_keys),
    }

    if fmt == "json":
        print(reporting.render_json(verdicts, scan_meta, newly_flaky, newly_resolved))
    elif fmt == "cron":
        new_flaky_verdicts = [v for v in verdicts if v.status == domain.VERDICT_FLAKY]
        text = reporting.render_cron(
            report_scope, new_flaky_verdicts=new_flaky_verdicts,
            newly_flaky=newly_flaky, newly_resolved=newly_resolved,
        )
        if text:
            print(text)
    else:  # human
        print(reporting.render_human(verdicts, scan_meta))
    return 0


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _cmd_status(args) -> int:
    from . import config, storage

    cfg = config.get_config()
    conn = storage.get_connection()
    last = storage.last_scan_run(conn)
    counts = storage.count_by_status(conn)

    print(f"verdicts_db:     {storage.get_db_path()}")
    print(f"config_path:     {config.config_path()}")
    print(f"test_history_db: {config.resolve_test_history_db_path(cfg)}")
    print("resolved config:")
    print(json.dumps(cfg, indent=2))
    if last:
        print(
            f"last scan:       {last['ran_at']} (window {last['window_days']}d, "
            f"min-fails {last['min_fails']}, include_errors={bool(last['include_errors'])}, "
            f"examined {last['tests_examined']}, flaky {last['flaky_found']}, "
            f"source_schema_version={last['source_schema_version']})"
        )
    else:
        print("last scan:       (never run — try `hermes flaky-detective scan`)")
    print(f"counts:          {counts if counts else '{}'}")
    return 0


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def _cmd_list(args) -> int:
    from . import domain, storage

    statuses = {
        "all": None,
        "flaky": [domain.VERDICT_FLAKY],
        "consistently_failing": [domain.VERDICT_CONSISTENTLY_FAILING],
    }[args.status]

    conn = storage.get_connection()
    rows = storage.read_verdicts(conn, statuses)
    if not rows:
        print("(no matching verdicts — run `hermes flaky-detective scan` first)")
        return 0
    for r in rows:
        print(
            f"{r['status']:>21}  {r['test_key']}  "
            f"[{r['fails']} fail / {r['passes']} pass of {r['runs']}]  "
            f"last_failure={r['last_failure'] or '-'}"
        )
    return 0


# ---------------------------------------------------------------------------
# install-cron (Phase 7)
# ---------------------------------------------------------------------------


SHIM_NAME = "flaky-scan.sh"
CRON_JOB_NAME = "flaky-detective"


def _printable_cron_command(schedule: str, deliver: str) -> str:
    return (f'hermes cron create "{schedule}" --no-agent --script {SHIM_NAME} '
            f'--deliver {deliver} --name {CRON_JOB_NAME}')


def _gateway_note() -> str:
    return ("Note: the gateway daemon must be running for the job to fire "
            "(`hermes gateway install`, then `hermes gateway`).")


def _install_shim() -> Path:
    """Copy the shim into ``<hermes_home>/scripts/`` with mode 0700; return its path."""
    import shutil

    from . import storage

    scripts_dir = storage.get_hermes_home() / "scripts"   # scripts must live here (§1.3)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(scripts_dir, 0o700)
    except OSError:
        pass
    shim_src = Path(__file__).resolve().parent / SHIM_NAME
    shim_dst = scripts_dir / SHIM_NAME
    shutil.copyfile(shim_src, shim_dst)
    try:
        os.chmod(shim_dst, 0o700)
    except OSError:
        pass
    return shim_dst


def _cmd_install_cron(args) -> int:
    """Install the nightly no-agent cron job (the only sanctioned subprocess).

    Steps: (a) copy the shim into ``~/.hermes/scripts/`` (0700); (b) persist the
    resolved options into ``config.json`` so the shim's ``scan`` uses them;
    (c) create the job once via ``hermes cron create``. If that CLI is missing or
    the gateway is not configured, this does **not** error — it prints the exact
    command plus a one-line gateway note. ``--no-create`` skips step (c).
    """
    from . import config, domain

    cfg = config.get_config()
    schedule = args.schedule or cfg.get("schedule", domain.DEFAULT_SCHEDULE)
    deliver = args.deliver or cfg.get("deliver", domain.DEFAULT_DELIVER)
    window_days = args.window if args.window is not None else int(cfg["window_days"])
    min_fails = args.min_fails if args.min_fails is not None else int(cfg["min_fails"])

    # Validate before persisting/installing anything, so a bad value is never
    # written into config.json nor baked into the scheduled job.
    try:
        _validate_tunables(window_days, min_fails)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # (b) persist resolved options so the scheduled `scan` uses them.
    config.write_config({
        "schedule": schedule, "deliver": deliver,
        "window_days": window_days, "min_fails": min_fails,
    })

    # (a) install the shim.
    shim_dst = _install_shim()
    print(f"installed shim: {shim_dst} (mode 0700)")

    printable = _printable_cron_command(schedule, deliver)

    # (d) honor --no-create: shim + config written; just print the command.
    if args.no_create:
        print("skipping job creation (--no-create). To create it later, run:")
        print(f"  {printable}")
        print(_gateway_note())
        return 0

    # (c) the one sanctioned subprocess: create the job once. A cron-run session
    # cannot create cron jobs, so this is only ever reached from the standalone CLI.
    import subprocess

    cron_cmd = ["hermes", "cron", "create", schedule, "--no-agent",
                "--script", SHIM_NAME, "--deliver", deliver, "--name", CRON_JOB_NAME]
    try:
        result = subprocess.run(cron_cmd, capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        print(f"could not run the hermes CLI ({exc}). To create the job, run:")
        print(f"  {printable}")
        print(_gateway_note())
        return 0

    if result.returncode == 0:
        out = (result.stdout or "").strip()
        if out:
            print(out)
        print(f"created cron job '{CRON_JOB_NAME}' (verify with `hermes cron list`).")
        return 0

    # Non-zero (e.g. gateway not configured): fall back to printing the command.
    detail = (result.stderr or result.stdout or "").strip()
    if detail:
        print(detail)
    print("could not create the cron job automatically. To create it, run:")
    print(f"  {printable}")
    print(_gateway_note())
    return 0
