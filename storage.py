"""Verdicts-DB lifecycle and profile-aware storage paths.

No class hierarchy — module-level functions plus one lazily-initialized singleton
(the connection). Storage paths derive from Hermes's profile-aware home, never
hardcoded, mirroring the sibling test-history plugin's ``storage.py`` (re-derived
here, not imported, to keep the GPL/data-only boundary intact).

Path helpers land in Phase 3 (the reader/config layer needs the home); the
verdicts-DB connection, schema application, and the transactional snapshot writes
land in Phase 4.
"""

import os
import sqlite3
from pathlib import Path

# Module-level singleton: the verdicts-DB connection, opened on first use and
# closed at process exit (and explicitly in tests via reset_for_tests).
_connection: sqlite3.Connection | None = None


# ---------------------------------------------------------------------------
# Paths (profile-aware)
# ---------------------------------------------------------------------------


def get_hermes_home() -> Path:
    """Return the profile-aware Hermes home.

    Prefer the official helper if Hermes is importable; otherwise fall back to
    the ``HERMES_HOME`` env var, then ``~/.hermes``. Never hardcoded.
    """
    try:
        from hermes_cli.utils import display_hermes_home  # type: ignore

        return Path(display_hermes_home())
    except Exception:
        # Hermes not importable (standalone dev/test) or helper moved.
        return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def get_storage_dir() -> Path:
    """The plugin's own storage directory (``<hermes_home>/flaky-detective``).

    Created on demand and kept owner-only: the verdicts DB records test
    identifiers captured from artifacts, so other local users should not be able
    to read it (or the WAL/-shm sidecars). Best-effort chmod: a filesystem that
    cannot represent POSIX modes (e.g. a Windows mount) must not break the plugin.
    """
    storage_dir = get_hermes_home() / "flaky-detective"
    storage_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(storage_dir, 0o700)
    except OSError:
        pass
    return storage_dir


def get_db_path() -> Path:
    """Path to this plugin's own verdicts database."""
    return get_storage_dir() / "verdicts.db"


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def get_connection() -> sqlite3.Connection:
    """Lazy singleton connection to the verdicts DB, with the schema applied.

    ``check_same_thread=False`` because the ``is_flaky`` tool may run in the
    gateway's async worker pool. WAL journal mode lets the tool's read-only
    lookups proceed concurrently with a ``scan`` writer in another process
    (CLI / nightly cron) instead of blocking on the rollback-journal lock.
    """
    global _connection
    if _connection is None:
        from .schema import apply_schema  # lazy import (keep startup cheap)

        db_path = get_db_path()
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # Owner-only: the DB records test identifiers captured from artifacts. The
        # 0o700 directory is the primary guard (covers the -wal/-shm sidecars);
        # this narrows the DB file itself. Best-effort on non-POSIX mounts.
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        apply_schema(conn)
        _connection = conn
    return _connection


def reset_for_tests() -> None:
    """Test-only hook: close and clear the module-level connection singleton."""
    global _connection
    if _connection is not None:
        _connection.close()
    _connection = None


# ---------------------------------------------------------------------------
# Verdict snapshot writes / reads
# ---------------------------------------------------------------------------

# Column order shared by the snapshot INSERT and the Verdict->row mapping.
# ``computed_at`` is omitted so its DEFAULT CURRENT_TIMESTAMP applies.
_VERDICT_COLS = (
    "test_key", "classname", "name", "file_path", "passes", "fails", "runs",
    "window_days", "first_seen", "last_seen", "last_failure", "status",
)
_VERDICT_INSERT = (
    "INSERT INTO flaky_verdicts (" + ", ".join(_VERDICT_COLS) + ") "
    "VALUES (" + ", ".join("?" * len(_VERDICT_COLS)) + ")"
)


def replace_verdicts(conn: sqlite3.Connection, verdicts) -> None:
    """Atomically replace the whole ``flaky_verdicts`` snapshot.

    A scan reflects the *current* state of the world, so the table is overwritten
    wholesale (delete + insert) inside a single transaction: readers see either
    the old snapshot or the new one, never a half-written mix.
    """
    params = [tuple(getattr(v, c) for c in _VERDICT_COLS) for v in verdicts]
    with conn:  # transaction: commit on success, rollback on error
        conn.execute("DELETE FROM flaky_verdicts")
        if params:
            conn.executemany(_VERDICT_INSERT, params)


def record_scan_run(conn: sqlite3.Connection, *, window_days: int, min_fails: int,
                    include_errors: bool, source_schema_version, tests_examined: int,
                    flaky_found: int) -> int:
    """Append one ``scan_runs`` audit row; return its id."""
    with conn:
        cur = conn.execute(
            "INSERT INTO scan_runs (window_days, min_fails, include_errors, "
            "source_schema_version, tests_examined, flaky_found) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(window_days), int(min_fails), 1 if include_errors else 0,
             source_schema_version, int(tests_examined), int(flaky_found)),
        )
    return int(cur.lastrowid)


def read_flaky_keys(conn: sqlite3.Connection) -> set:
    """The set of ``test_key`` currently classified ``flaky`` (for change diffs)."""
    from . import domain  # lazy: domain is import-free, this just avoids top churn

    rows = conn.execute(
        "SELECT test_key FROM flaky_verdicts WHERE status = ?",
        (domain.VERDICT_FLAKY,),
    ).fetchall()
    return {r["test_key"] for r in rows}


def get_verdict(conn: sqlite3.Connection, test_key: str):
    """The verdict row for ``test_key``, or ``None`` if there is none."""
    return conn.execute(
        "SELECT * FROM flaky_verdicts WHERE test_key = ?", (test_key,)
    ).fetchone()


def read_verdicts(conn: sqlite3.Connection, statuses=None) -> list:
    """All verdict rows, optionally filtered to ``statuses`` (an iterable).

    Ordered worst-first (most failures, then most runs, then key) for stable,
    human-friendly listings.
    """
    sql = "SELECT * FROM flaky_verdicts"
    params: tuple = ()
    if statuses:
        statuses = tuple(statuses)
        placeholders = ", ".join("?" * len(statuses))
        sql += f" WHERE status IN ({placeholders})"
        params = statuses
    sql += " ORDER BY fails DESC, runs DESC, test_key ASC"
    return conn.execute(sql, params).fetchall()


def last_scan_run(conn: sqlite3.Connection):
    """The most recent ``scan_runs`` row, or ``None`` if no scan has run."""
    return conn.execute(
        "SELECT * FROM scan_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()


def count_by_status(conn: sqlite3.Connection) -> dict:
    """``{status: count}`` over ``flaky_verdicts`` (empty dict if none)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM flaky_verdicts GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}
