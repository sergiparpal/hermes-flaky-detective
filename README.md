# hermes-flaky-detective

A [Hermes Agent](https://hermes-agent.nousresearch.com) **general** plugin that
detects **flaky tests** — tests that both pass *and* fail within a recent window
— by reading the SQLite database produced by the
[`hermes-test-history`](https://github.com/sergiparpal/hermes-test-history)
plugin. It persists its verdicts to its own local database, exposes an
**`is_flaky`** tool to the agent, and runs a **nightly no-agent cron sweep** that
reports changes at **zero LLM cost**.

It never modifies Hermes core, makes no network calls, uses only the Python
standard library, and runs no subprocess except the single `install-cron`
command.

---

## What it does

- **Reads** `hermes-test-history`'s SQLite database **read-only** (`mode=ro`),
  one logical run per `(source_file, run_timestamp)` to undo test-history's lack
  of ingest dedup, excluding `skipped` cases.
- **Classifies** each test over a window:
  - **`flaky`** — `fails >= min_fails` **and** `passes >= 1` (intermittent).
  - **`consistently_failing`** — `fails >= min_fails` **and** `passes == 0`
    (a real break, not flaky).
  - **`stable`** — otherwise (`fails < min_fails`).
- **Persists** the latest verdicts and an audit row per scan to its own DB.
- **Exposes** the `is_flaky` tool so the agent can ask "is this test known-flaky?"
  while triaging a failure.
- **Reports** nightly via a no-agent cron job: in `changes-only` mode it prints
  only newly-flaky / newly-resolved tests, and stays silent on quiet nights.

---

## Install

User-scope plugins live in `~/.hermes/plugins/` and are discovered automatically
at startup. Either:

```bash
hermes plugins install <you>/hermes-flaky-detective --enable
```

or clone and symlink into the plugins directory:

```bash
git clone https://github.com/<you>/hermes-flaky-detective ~/src/hermes-flaky-detective
ln -s ~/src/hermes-flaky-detective ~/.hermes/plugins/hermes-flaky-detective
hermes plugins enable hermes-flaky-detective   # if not auto-enabled
```

Requires **Python 3.12+**, a working Hermes install, and the
**`hermes-test-history`** plugin installed and ideally populated (`hermes
test-history ingest <path>`). The plugin and its tests need no third-party
dependencies.

---

## First run

```bash
# Run a detection sweep and print a human report (or a clean "no data" message).
hermes flaky-detective scan --format human

# Show resolved config, DB paths, the last scan, and the observed schema version.
hermes flaky-detective status

# List the stored verdicts.
hermes flaky-detective list --status flaky
```

`scan` flags: `--window N`, `--min-fails N`,
`--include-errors/--no-include-errors`, `--format human|cron|json`
(each defaults to the configured value).

---

## The `is_flaky` tool

Registered in the `flaky_detective` toolset. The agent calls it while triaging a
single failing test to decide "known-flaky (safe to retry) or a real
regression?".

- **Argument**: `test_id` — a bare test name, `classname::name`, or
  `file_path::name`.
- **Found**:
  ```json
  {"success": true, "test_id": "...", "test_key": "...", "is_flaky": true,
   "status": "flaky", "fails": 3, "passes": 4, "runs": 7, "window_days": 14,
   "last_failure": "...", "computed_at": "...", "content_warning": "..."}
  ```
- **No verdict on record**:
  ```json
  {"success": true, "is_flaky": false, "status": "unknown",
   "note": "No verdict on record; run `hermes flaky-detective scan` or wait for the nightly job."}
  ```
- **Bad input**: `{"success": false, "error": "...", "remediation": "..."}`

Verdicts come from the **most recent scan**; run `scan` (or let the nightly job
run) before relying on the tool. Test identifiers in the result are captured from
test artifacts and are surfaced with a `content_warning` so the model treats them
as data, never instructions.

---

## The nightly cron job

```bash
hermes flaky-detective install-cron --schedule "0 9 * * *" --deliver local
```

This:

1. copies the shim `flaky-scan.sh` into `~/.hermes/scripts/` (mode `0700`);
2. persists the resolved options into the plugin's `config.json`;
3. creates a **no-agent** cron job once (the only subprocess this plugin runs):
   ```bash
   hermes cron create "0 9 * * *" --no-agent --script flaky-scan.sh --deliver local --name flaky-detective
   ```

If the `hermes` CLI is unavailable or the gateway is not configured, it does
**not** error — it prints the exact command plus a note that the gateway daemon
must be running (`hermes gateway install`, then `hermes gateway`). Use
`--no-create` to only install the shim + config and print the command.

The shim runs `hermes flaky-detective scan --format cron`. A no-agent job
delivers its **stdout verbatim** at zero LLM cost; **empty stdout is a silent
tick** (the desired behavior on nights with no flaky-test changes), while a
non-zero exit makes Hermes deliver an error alert so a broken sweep cannot fail
silently.

---

## Configuration

Optional `~/.hermes/flaky-detective/config.json`; missing keys fall back to
defaults. `hermes flaky-detective status` prints the resolved config.

| Key | Default | Meaning |
|---|---|---|
| `window_days` | `14` | Detection look-back window. |
| `min_fails` | `3` | Minimum failures in the window to be non-stable. |
| `include_errors` | `true` | Count `error` status as a failure (alongside `failed`). |
| `deliver` | `"local"` | Cron delivery channel (`local`/`slack`/`telegram`/`discord`/…). |
| `schedule` | `"0 9 * * *"` | Cron schedule (daily 09:00). |
| `report_scope` | `"changes-only"` | `changes-only` (diff; silent when unchanged) or `full-set`. |
| `test_history_db_path` | `null` | Override; `null` → `~/.hermes/test-history/history.db`. |
| `source_schema_version` | `1` | The test-history `schema_version` this plugin targets. |

---

## Storage & privilege surface

- **Reads** the test-history database **read-only** (never opened writable).
- **Reads/writes** `~/.hermes/flaky-detective/` (its own `verdicts.db` + config),
  kept owner-only: directory `0700`, database & config `0600` (best-effort on
  filesystems that cannot represent POSIX modes).
- All SQL is **parameterized**; no values are interpolated into SQL.
- **No network**, **no third-party runtime dependencies**, and **no subprocess**
  except `install-cron`'s single `hermes cron create`.
- The `is_flaky` tool logs internal errors server-side and returns a generic
  message — no filesystem paths or SQL text reach the model.

---

## Coupling to `hermes-test-history`

This plugin reads another plugin's schema, which is a coupling point. It targets
`schema_version == 1` and **warns** (without failing) if the installed
test-history DB reports a different version. Known behavior inherited from
test-history v0.1.0: re-ingesting the same file creates a new run, so this plugin
**dedups logical runs by `(source_file, run_timestamp)`** at read time. Some
emitters (e.g. jest) omit `file_path`, so tests are identified by
`classname::name` (path-based filtering will miss those).

**Dedup needs a `run_timestamp`.** Because the dedup key falls back to
`ingested_at` when a run has no `run_timestamp`, two ingests of the same
timestamp-less artifact look like two distinct runs and **cannot** be collapsed —
their cases are counted once per ingest, which can inflate a test's fail count.
When this is detected (an in-window `source_file` with more than one
timestamp-less run) the scan logs a one-line warning to stderr; fix it by ensuring
your JUnit XML carries a suite `timestamp`, or by pruning the duplicate runs in
test-history.

### GPL / data-only boundary

`hermes-test-history` is **GPL-3.0**. This plugin only reads its SQLite **data
file** — which does **not** create a derivative work — and **never imports its
Python modules**. The few shared bits (the effective-timestamp expression, the
schema columns used by the reader and test fixtures) are **re-implemented
locally**, so this plugin stays under its own license.

---

## Development

```bash
scripts/run_tests.sh            # CI-parity wrapper (TZ=UTC, creds unset) -> pytest
scripts/run_tests.sh tests/test_detect.py
```

The suite needs **no live Hermes**: registration uses a fake plugin context, and
storage/reader resolve into a temporary `HERMES_HOME`. The pure detection core
(`detect.py`) does no I/O and is unit-tested with plain tuples.

Layout: `detect.py` (pure classification), `query.py` (read-only test-history
reader), `storage.py` + `schema.py` (verdicts DB), `config.py` (config + path
resolution), `domain.py`/`timeutil.py` (shared rules), `reporting.py` (renderers),
`cli.py` (`scan`/`status`/`list`/`install-cron`), `__init__.py` (`register` +
the `is_flaky` tool), `flaky-scan.sh` (cron shim).
