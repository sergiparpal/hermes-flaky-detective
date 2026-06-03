# Implementation Plan — `hermes-flaky-detective`

A Hermes Agent plugin that detects flaky tests by reading the SQLite database
produced by the **`hermes-test-history`** plugin, persisting verdicts to its own
local database, exposing an `is_flaky` tool to the agent, and running a nightly
detection sweep as a **no-agent cron job**.

This document is written to be executed end-to-end by an autonomous coding agent
(Claude Code CLI). Each phase ends with **machine-checkable acceptance criteria**
so the agent can self-verify and proceed without a human gate. The only human
interaction is a single batched questionnaire in **Phase 0** (every question has
a default; if the user defers, apply defaults and continue).

---

## 0. How to use this plan (instructions for the implementing agent)

1. Work top to bottom. Do **not** skip the "Ground truth" reading in Phase 1 —
   it replaces guessing at the Hermes plugin API.
2. After each phase, run that phase's acceptance check. If it fails, fix and
   re-run before moving on. Do not ask the human to verify a phase.
3. The plugin must **never modify Hermes core files**. All code lives under the
   plugin directory and `~/.hermes/scripts/`.
4. Prefer the standard library. No third-party runtime dependencies. No network
   calls. No subprocess calls **except** in the `install-cron` CLI command
   (clearly scoped in Phase 7).
5. All SQL is parameterized. The test-history database is opened **read-only**.
6. If a referenced Hermes API detail is ambiguous, resolve it by reading the
   installed `hermes-test-history` source (a known-good sibling plugin) and the
   official guide, in that order. Do not invent manifest fields or `ctx` methods.

### Authoritative references (fetch only if a detail is ambiguous)

- Build a Hermes Plugin: <https://hermes-agent.nousresearch.com/docs/guides/build-a-hermes-plugin>
- Plugins feature: <https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins>
- Built-in Plugins (discovery/enable rules): <https://hermes-agent.nousresearch.com/docs/user-guide/features/built-in-plugins>
- Scheduled Tasks (Cron): <https://hermes-agent.nousresearch.com/docs/user-guide/features/cron>
- Sibling reference plugin (the dependency): <https://github.com/sergiparpal/hermes-test-history>

---

## 1. Technical requirements & environment

### 1.1 Runtime requirements

- **Python 3.12+** (the `hermes-test-history` dependency requires it; match it).
- **SQLite with FTS5** available in the Python build (standard in CPython). This
  plugin does not create an FTS index, but the environment provides one.
- **Standard library only** at runtime: `sqlite3`, `argparse`, `json`,
  `datetime`, `pathlib`, `os`, `stat`, `logging`. (No `requests`, no ORM.)
- A working **Hermes Agent** install (target the current release; verify with
  `hermes --version`). At time of writing the latest is `v0.15.2`.
- The **`hermes-test-history` plugin installed and enabled**, ideally with at
  least one ingested JUnit XML run so real detection has data. The build and the
  test suite do **not** require real data (they use synthetic fixtures).

### 1.2 Hermes plugin contract (what Hermes needs from this plugin)

- **Location**: a user-scope plugin directory at
  `~/.hermes/plugins/hermes-flaky-detective/`. User-scope plugins are discovered
  and loaded automatically at startup (discovery scans bundled → user → project
  → pip entry points, with later sources winning on name collision).
- **Manifest**: a `plugin.yaml` at the plugin root. Treat the **installed
  `hermes-test-history/plugin.yaml` as the canonical template** and mirror its
  field set and `category: general`. Do not add speculative fields such as
  `requires_hermes_version` (it does not exist). If the plugin must be gated on
  an environment variable, that is the `requires_env` manifest field.
- **Entry point**: `__init__.py` exposing a `register(ctx)` function. Confirm the
  exact registration signature from `hermes-test-history/__init__.py`.
- **Registration APIs used** (confirm exact signatures from the sibling plugin
  and the official guide):
  - `ctx.register_tool(name, toolset, schema, handler, check_fn=None)` — exposes
    a tool to the LLM. The human-readable **`description` lives inside the
    `schema` dict** (OpenAI function-calling style: `name`, `description`,
    `parameters`), **not** as a separate kwarg.
  - `ctx.register_cli_command(name, help, setup_fn, handler_fn)` — adds
    `hermes flaky-detective <subcommand>`.
- **Not used by this plugin** (do not add): `ctx.register_memory_provider`
  (this is a `general` plugin, not a memory provider), `ctx.llm` (detection is
  deterministic — no LLM calls), `ctx.dispatch_tool` (we read the DB directly;
  see §3.1 for the rationale).
- **Privilege/security posture** (mirror test-history): owner-only storage
  (`0700` directory, `0600` database file), parameterized SQL everywhere,
  profile-aware path resolution (never hardcode `~/.hermes`), no network, and no
  subprocess except the scoped `install-cron` command.

### 1.3 Cron subsystem facts (what the nightly run relies on)

- Hermes has **no plugin-side cron registration API**. Scheduling is a separate
  subsystem driven by the `cronjob` tool / `/cron` slash command / `hermes cron`
  CLI, executed by the **gateway daemon** (ticks every 60s, runs jobs in fresh
  isolated sessions, stores jobs in `~/.hermes/cron/jobs.json`).
- This plugin uses **no-agent mode**: a script in `~/.hermes/scripts/` runs on a
  schedule and its **stdout is delivered verbatim**, with **zero LLM cost**.
  - **Empty stdout → silent tick** (no delivery). This is the desired behavior on
    nights with no new flaky results.
  - Non-zero exit or timeout → Hermes delivers an error alert (so a broken sweep
    cannot fail silently).
  - Default script timeout is 120s (raise via `cron.script_timeout_seconds` in
    `config.yaml` or `HERMES_CRON_SCRIPT_TIMEOUT` if ever needed).
- Scripts **must** live in `~/.hermes/scripts/`. `.sh`/`.bash` run under bash;
  other files run under the current Python interpreter.
- A cron-run session **cannot create more cron jobs**, so the job must be created
  once from the standalone CLI (Phase 7), never from inside a scheduled run.

---

## 2. Phase 0 — Configuration questionnaire (single batched prompt)

Ask the user **once**, as a single batched question set, using Claude Code's
interactive question mechanism. Each item has a default. If the user does not
answer promptly, **apply the defaults and continue** — do not block.

| # | Question | Options | Default |
|---|----------|---------|---------|
| 1 | Where should the nightly summary be delivered? | `local`, `slack`, `telegram`, `discord`, `all`, or a comma list | `local` (saves to `~/.hermes/cron/output/`, needs no messaging setup) |
| 2 | Should `error`-status results count as failures for flakiness? | yes / no | `yes` |
| 3 | Detection window (days) and minimum failures | integers | `14` days, `3` failures |
| 4 | Cron schedule | cron expr or interval | `0 9 * * *` (daily 09:00) |
| 5 | At the end, auto-create the cron job? | yes / no | `yes` (fall back to printing the command if the gateway is not configured) |
| 6 | Nightly report scope | `changes-only` / `full-set` | `changes-only` (report newly flaky + newly resolved; silent when nothing changed) |

Record the chosen values (or defaults) into the plugin's `config.json` defaults
(see §5.6) so the rest of the build is parameterized by them.

**Acceptance check (Phase 0):** the chosen configuration is written to a scratch
file (e.g., `./.flaky-plan-config.json`) and echoed back. Proceed.

---

## 3. Architecture

### 3.1 Why read the test-history DB directly (design rationale)

The two tools test-history exposes (`test_failure_lookup`, `module_failure_history`)
are **failure-centric summaries** for interactive agent reasoning. Neither returns,
per test and within a time window, the **pass+fail counts** the flaky heuristic
needs (intermittent = failed several times **and** also passed). The test-history
**schema does** store this (per-case `status` + per-run timestamp), and its README
designates "read the same SQLite file" as the intended composition path for a
flaky-detective. The database runs in **WAL mode**, so read-only queries are safe
to run concurrently with ingestion. Therefore this plugin reads the test-history
DB directly and does **not** call `ctx.dispatch_tool` and does **not** re-parse
JUnit XML.

**Coupling note:** reading another plugin's schema is a coupling point. Pin to
`schema_version == 1` (see §4) and warn if it differs.

**License note (GPL-3.0):** `hermes-test-history` is GPL-3.0. Reading its SQLite
**data file** does not create a derivative work, so this plugin is unaffected.
**Do not `import` test-history's Python modules** — that would combine with GPL
code and make this plugin GPL-3.0. Re-implement the small bits we need (e.g. the
"effective timestamp" expression) locally.

### 3.2 The two artifacts

1. **The plugin** (`~/.hermes/plugins/hermes-flaky-detective/`) — provides the
   detection library, its own verdicts database, the `is_flaky` tool, and the
   `hermes flaky-detective` CLI commands.
2. **A no-agent cron job** — created once; runs a thin shim script that calls
   `hermes flaky-detective scan` nightly and delivers the summary.

### 3.3 File layout

```
~/.hermes/plugins/hermes-flaky-detective/
├── plugin.yaml            # manifest (mirror test-history; category: general)
├── __init__.py            # register(ctx): registers is_flaky tool + CLI command
├── cli.py                 # hermes flaky-detective <scan|status|list|install-cron>
├── detect.py              # PURE detection logic (no I/O, no SQL) — the testable core
├── query.py               # read-only reader of the test-history DB
├── storage.py             # own verdicts DB lifecycle + profile-aware paths
├── schema.py              # verdicts DDL + schema_version
├── domain.py              # statuses, effective-timestamp SQL, tunable defaults
├── timeutil.py            # ISO-8601 normalization (compare timestamps identically)
├── config.py              # config.json resolution (defaults + overrides)
├── reporting.py           # render the human summary / changes diff
├── tests/
│   ├── conftest.py        # fake plugin context + temp HERMES_HOME (mirror test-history)
│   ├── fixtures.py        # helpers to build a synthetic test-history DB
│   ├── test_detect.py     # the most important suite (pure logic)
│   ├── test_query.py      # reading + dedup against a synthetic DB
│   ├── test_cli.py        # scan/status/list end-to-end on temp homes
│   └── test_registration.py  # register(ctx) wires tool + CLI correctly
├── scripts/
│   └── run_tests.sh       # CI-parity wrapper (TZ=UTC, LANG=C.UTF-8, unset creds)
├── flaky-scan.sh          # cron shim template (installed into ~/.hermes/scripts/)
├── README.md
└── LICENSE
```

---

## 4. Reference: the `hermes-test-history` schema (read-only input)

This is the schema this plugin reads. **Re-derive it at build time** by reading
the installed `hermes-test-history/schema.py` and confirm `schema_version == 1`;
the summary below is for orientation.

```sql
test_runs(
  id INTEGER PK, suite_name TEXT, ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  run_timestamp TIMESTAMP NULL,           -- from XML if present, else NULL
  total INT, failures INT, errors INT, skipped INT,
  source_file TEXT                        -- absolute path of the ingested XML
)

test_cases(
  id INTEGER PK, run_id INTEGER FK -> test_runs(id) ON DELETE CASCADE,
  classname TEXT, name TEXT NOT NULL, file_path TEXT NULL, line_number INT,
  status TEXT,                            -- 'passed' | 'failed' | 'error' | 'skipped'
  duration_ms REAL, failure_message TEXT, failure_type TEXT, stack_trace TEXT
)

schema_version(version INTEGER PK, applied_at TIMESTAMP)
```

Key consequences for detection:

- **Effective timestamp** for windowing = `COALESCE(run_timestamp, ingested_at)`.
- **`skipped` must be excluded** from both pass and fail counts.
- **`failed` vs `error`** are distinct case statuses; whether `error` counts as a
  failure is governed by Phase 0 question 2 (default: yes).
- **No ingest dedup** in test-history v0.1.0: re-ingesting the same file creates a
  new `test_runs` row. To avoid inflating counts, **dedup logical runs by
  `(source_file, run_timestamp)`** at read time (see §5.3).
- **jest** emits no file attribute → `file_path` is NULL; identify tests by
  `(classname, name)` so jest still works (module/path filtering will miss jest).

---

## 5. The detection design (precise spec)

### 5.1 Test identity

Canonical key: `test_key = f"{classname}::{name}"` (use `"::{name}"` when
`classname` is NULL/empty). Keep `classname`, `name`, and `file_path` alongside
for reporting.

### 5.2 Verdict classification

Given, within the window, per test: `passes`, `fails` (where `fails` counts
`failed` and, if enabled, `error`), and `runs = passes + fails`:

- **`flaky`** ⟺ `fails >= min_fails` **and** `passes >= 1`
- **`consistently_failing`** ⟺ `fails >= min_fails` **and** `passes == 0`
- **`stable`** ⟺ otherwise (`fails < min_fails`)

`is_flaky` is true only for the `flaky` status.

### 5.3 Read query (in `query.py`, against the test-history DB, read-only)

Open with a read-only URI: `sqlite3.connect("file:<db>?mode=ro", uri=True)`.

```sql
-- 1) Determine one logical run per (source_file, run_timestamp) to dedup reingests.
WITH logical_runs AS (
  SELECT id, source_file,
         COALESCE(run_timestamp, ingested_at) AS eff_ts,
         ROW_NUMBER() OVER (
           PARTITION BY source_file, COALESCE(run_timestamp, ingested_at)
           ORDER BY ingested_at DESC, id DESC
         ) AS rn
  FROM test_runs
  WHERE COALESCE(run_timestamp, ingested_at) >= :cutoff
)
SELECT c.classname, c.name, c.file_path, c.status, lr.eff_ts
FROM test_cases c
JOIN logical_runs lr ON lr.id = c.run_id AND lr.rn = 1
WHERE c.status <> 'skipped';
```

If the SQLite build lacks window functions, fall back to a two-step approach:
select the distinct surviving `run_id`s in Python, then query their cases.

`:cutoff` = `now - window_days` as an ISO-8601 string, normalized via
`timeutil.py` so it compares correctly against stored timestamps.

### 5.4 Pure detection core (in `detect.py`)

```
compute_verdicts(rows, now, window_days, min_fails, include_errors) -> list[Verdict]
```

- Input `rows`: iterable of `(classname, name, file_path, status, eff_ts)`.
  **No DB access here** — this function is pure and fully unit-tested.
- Group by `test_key`; tally `passes` / `fails` (per §5.2); track
  `first_seen`, `last_seen`, `last_failure`.
- Emit a `Verdict` per test with its computed `status`.
- This separation (pure core vs I/O in `query.py`/`storage.py`) is the same
  discipline test-history uses; keep it.

### 5.5 Verdicts database (in `schema.py` / `storage.py`)

Stored under `<hermes_home>/flaky-detective/` (`0700` dir, `0600` DB), path
resolved profile-aware exactly the way test-history resolves its own home
(read its `storage.py` to copy the resolution logic — re-implement, don't import).

```sql
CREATE TABLE IF NOT EXISTS flaky_verdicts (
  test_key     TEXT PRIMARY KEY,
  classname    TEXT,
  name         TEXT NOT NULL,
  file_path    TEXT,
  passes       INTEGER NOT NULL,
  fails        INTEGER NOT NULL,
  runs         INTEGER NOT NULL,
  window_days  INTEGER NOT NULL,
  first_seen   TIMESTAMP,
  last_seen    TIMESTAMP,
  last_failure TIMESTAMP,
  status       TEXT NOT NULL,          -- 'flaky' | 'consistently_failing' | 'stable'
  computed_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  ran_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  window_days     INTEGER NOT NULL,
  min_fails       INTEGER NOT NULL,
  include_errors  INTEGER NOT NULL,
  source_schema_version INTEGER,        -- test-history schema_version observed
  tests_examined  INTEGER NOT NULL,
  flaky_found     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER PRIMARY KEY,
  applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);
```

A `scan` replaces the `flaky_verdicts` contents transactionally (delete + insert,
or upsert then prune absent keys) and appends one `scan_runs` row. To support
`changes-only` reporting (Phase 0 Q6), compute the diff against the previous
`flaky_verdicts` snapshot **before** overwriting (read current `flaky` keys,
compute the new set, diff, then persist).

### 5.6 Config resolution (in `config.py`)

Optional `<hermes_home>/flaky-detective/config.json`, defaults filled for missing
keys. Keys: `window_days` (14), `min_fails` (3), `include_errors` (true),
`deliver` ("local"), `schedule` ("0 9 * * *"), `report_scope` ("changes-only"),
`test_history_db_path` (null → auto-resolve), `source_schema_version` (1).
`hermes flaky-detective status` prints the resolved config (mirror
`hermes test-history config`).

Locate the test-history DB path by: (a) `test_history_db_path` override if set;
else (b) the profile-aware `<hermes_home>/test-history/<dbfile>` path read from
test-history's installed `storage.py`; else (c) parse the path printed by
`hermes test-history status`.

---

## 6. Phased implementation with acceptance criteria

> Each phase is self-verifying. Run the acceptance check; if it fails, fix and
> re-run; then continue. No human gate between phases.

### Phase 1 — Ground truth & scaffold

1. Verify environment: `hermes --version`; `python3 --version` (must be ≥3.12);
   confirm `hermes plugins list` shows `hermes-test-history` enabled.
2. Read the installed `hermes-test-history` source as the API template:
   `plugin.yaml`, `__init__.py`, `cli.py`, `storage.py`, `schema.py`,
   `domain.py`, `scripts/run_tests.sh`, `tests/conftest.py`. Note the exact
   `register(ctx)` signature, the `register_tool`/`register_cli_command` call
   shapes, the home-resolution helper, and the test-context fake.
3. Create the directory tree from §3.3 with empty/stub modules and a
   `plugin.yaml` cloned from test-history's (rename to `hermes-flaky-detective`,
   `category: general`, version `0.1.0`).
4. Add `LICENSE` (your choice — this plugin is independent of test-history's GPL
   because it only reads its data file) and a `README.md` stub.

**Acceptance:** `hermes plugins list` shows `hermes-flaky-detective` discovered;
`hermes plugins enable hermes-flaky-detective` (if not auto-enabled) succeeds and
`hermes` starts without import errors. `python3 -c "import ast,glob; [ast.parse(open(f).read()) for f in glob.glob('*.py')]"` passes in the plugin dir.

### Phase 2 — Pure detection core (`detect.py` + tests)

1. Implement `compute_verdicts(...)` per §5.4 with no I/O.
2. Implement `domain.py` (status constants, defaults, the effective-timestamp
   SQL string) and `timeutil.py` (ISO normalization).
3. Write `tests/test_detect.py` covering: a clean flaky case (3 fails + ≥1 pass);
   consistently-failing (≥3 fails, 0 pass); stable (<3 fails); `error` counted vs
   not counted (both `include_errors` modes); `skipped` ignored; a test exactly at
   the boundary; out-of-window rows excluded by the caller (verify the core
   trusts pre-filtered input and the window filtering is asserted in `query` tests).

**Acceptance:** `pytest tests/test_detect.py` passes; `detect.py` imports nothing
outside the stdlib and performs no file/DB/network I/O (grep for `sqlite3`,
`open(`, `socket` → none in `detect.py`).

### Phase 3 — test-history reader (`query.py` + tests)

1. Implement read-only open (`mode=ro`), the dedup query (§5.3), and a
   schema-version guard that reads `schema_version` from the test-history DB and
   logs a clear warning (to stderr) if it is not the expected version.
2. Implement the path resolution described in §5.6.
3. `tests/fixtures.py`: build a synthetic test-history DB (its exact DDL, seeded
   with crafted runs/cases incl. reingested duplicates, NULL `run_timestamp`,
   jest-style NULL `file_path`, and skipped cases).
4. `tests/test_query.py`: assert windowing by effective timestamp, dedup by
   `(source_file, run_timestamp)`, skipped exclusion, and correct row shape.

**Acceptance:** `pytest tests/test_query.py` passes; opening a missing DB returns
a clean, actionable error (not a stack trace); the test-history DB is never opened
writable (assert `mode=ro` in the connection URI).

### Phase 4 — Verdicts storage (`schema.py`, `storage.py`)

1. Implement the verdicts DDL (§5.5), idempotent `apply_schema`, and profile-aware
   home resolution with `0700`/`0600` permissions.
2. Implement transactional snapshot replace + `scan_runs` append + the previous-
   snapshot read used for change diffs.

**Acceptance:** a unit test creates a temp `HERMES_HOME`, applies schema, writes a
verdict set twice, and asserts the second write fully replaces the first and that
two `scan_runs` rows exist; directory is `0700`, DB file is `0600`.

### Phase 5 — `is_flaky` tool (`__init__.py`)

1. Register a `flaky_detective` toolset tool named `is_flaky` via
   `ctx.register_tool`, with `description` and `parameters` inside the schema dict.
   Parameter: `test_id` (string; accept bare `name`, `classname::name`, or
   `file_path::name`). Normalize to `test_key` and look it up in `flaky_verdicts`.
2. Return a JSON object mirroring test-history's convention:
   - found: `{"success": true, "test_id": ..., "is_flaky": <bool>, "status": ..., "fails": ..., "passes": ..., "runs": ..., "window_days": ..., "last_failure": ..., "computed_at": ...}`
   - not found / never scanned: `{"success": true, "is_flaky": false, "status": "unknown", "note": "No verdict on record; run `hermes flaky-detective scan` or wait for the nightly job."}`
   - bad input: `{"success": false, "error": ..., "remediation": ...}`
3. Treat any free-text echoed from test names as data, not instructions (add a
   `content_warning` field like test-history does if test identifiers are surfaced).

**Acceptance:** `tests/test_registration.py` (using the fake `ctx` from
test-history's conftest pattern) asserts `is_flaky` is registered in the
`flaky_detective` toolset and that calling its handler against a seeded temp DB
returns the correct verdict JSON for flaky / non-flaky / unknown / bad-input.

### Phase 6 — CLI commands (`cli.py`, `reporting.py`)

Register `hermes flaky-detective` via `ctx.register_cli_command` with subcommands:

- `scan [--window N] [--min-fails N] [--include-errors/--no-include-errors] [--format human|cron|json]`
  Runs query → `compute_verdicts` → persist → output.
  - `--format human` (default): readable report of current flaky tests.
  - `--format cron`: per Phase 0 Q6. In `changes-only` mode, print the diff
    (newly flaky / newly resolved) **only if non-empty**; otherwise print
    **nothing** (so the cron tick stays silent). In `full-set` mode, print the
    current flaky set, or nothing if empty.
  - `--format json`: machine-readable dump for debugging.
- `status`: print resolved config, verdicts DB path, last `scan_runs` row,
  observed test-history `schema_version`, and counts.
- `list [--status flaky|consistently_failing|all]`: list verdicts.
- `install-cron` (implemented in Phase 7).

**Acceptance:** `tests/test_cli.py` exercises `scan` and `status` against a temp
`HERMES_HOME` with a synthetic test-history DB and asserts: correct flaky
detection end-to-end; `--format cron` prints nothing when there are no changes;
exit code 0 on success. Manually: `hermes flaky-detective status` runs against the
real install without error.

### Phase 7 — Cron wiring (`flaky-scan.sh`, `install-cron`)

1. Ship `flaky-scan.sh` (the shim) in the plugin dir:
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail
   exec hermes flaky-detective scan --format cron
   ```
2. Implement `hermes flaky-detective install-cron [--schedule ...] [--deliver ...]
   [--window N] [--min-fails N]` to:
   a. Ensure `~/.hermes/scripts/` exists; copy `flaky-scan.sh` there with mode
      `0700`.
   b. Persist the resolved options into `config.json` (so the shim's `scan` uses
      them).
   c. Create the job **once** from the standalone CLI. This is the **only**
      sanctioned `subprocess` call in the plugin:
      ```
      hermes cron create "<schedule>" --no-agent --script flaky-scan.sh \
        --deliver "<channel>" --name flaky-detective
      ```
      If that command fails (e.g., gateway not configured), **do not error out** —
      print the exact command and a one-line note that the gateway must be running
      (`hermes gateway install` / `hermes gateway`) for the job to fire.
   d. Honor Phase 0 Q5: if the user chose not to auto-create, only write the shim
      and print the command.

**Acceptance:** `~/.hermes/scripts/flaky-scan.sh` exists and is executable;
`hermes flaky-detective install-cron --deliver local` either creates a job
visible in `hermes cron list` **or** prints the ready-to-run command plus the
gateway note. Running the shim directly (`bash ~/.hermes/scripts/flaky-scan.sh`)
prints either a summary or nothing, and exits 0.

### Phase 8 — Test harness, hardening, docs

1. `scripts/run_tests.sh`: a CI-parity wrapper modeled on test-history's
   (pins `TZ=UTC`, `LANG=C.UTF-8`, unsets credential env vars, runs `pytest`).
   The suite must need no live Hermes install (registration uses the fake ctx;
   storage resolves into a temp `HERMES_HOME`).
2. Hardening review: confirm parameterized SQL only; test-history DB opened
   read-only; no network; no subprocess outside `install-cron`; owner-only perms;
   internal errors logged server-side and returned generically by the tool (no
   path/SQL leakage to the model).
3. Write `README.md`: what it does, install (`hermes plugins install
   <you>/hermes-flaky-detective --enable` or clone into `~/.hermes/plugins/`),
   first run (`hermes flaky-detective scan`), the `is_flaky` tool, the cron job,
   config keys, the test-history schema-version coupling, and the GPL/data-only
   boundary note.

**Acceptance:** `scripts/run_tests.sh` passes the whole suite from a clean
checkout with no credentials and no running gateway. `grep -rn "import.*test_history\|from test_history\|hermes_test_history"` in the plugin → no matches (GPL boundary intact).

---

## 7. Definition of done (final self-check, no human gate)

Run all of the following; all must pass:

- [ ] `hermes plugins list` shows `hermes-flaky-detective` enabled.
- [ ] `hermes` starts cleanly (no import/registration errors).
- [ ] `scripts/run_tests.sh` passes the full suite offline.
- [ ] `hermes flaky-detective status` prints resolved config + DB path + schema
      version without error.
- [ ] `hermes flaky-detective scan --format human` runs against the real
      test-history DB (or reports "no data" cleanly if none ingested yet).
- [ ] The `is_flaky` tool is callable by the agent and returns the documented
      JSON shapes (flaky / non-flaky / unknown / bad-input).
- [ ] `~/.hermes/scripts/flaky-scan.sh` exists, is `0700`, and exits 0 when run.
- [ ] The cron job exists in `hermes cron list`, **or** `install-cron` printed the
      exact creation command plus the gateway note (per Phase 0 Q5).
- [ ] No third-party runtime deps; no network; no subprocess outside
      `install-cron`; test-history DB opened read-only; no test-history Python
      imports.

---

## 8. Decision points (resolved by defaults; no mid-plan blocking)

These were decided in Phase 0 and are recorded here for traceability. None of
them require stopping the implementation; defaults apply if unanswered.

- **`error` counts as failure** — default yes.
- **Window / threshold** — default 14 days / ≥3 failures.
- **Delivery channel** — default `local`.
- **Schedule** — default `0 9 * * *`.
- **Report scope** — default `changes-only` (silent when nothing changed).
- **Reingest dedup key** — fixed at `(source_file, run_timestamp)`; documented as
  a known limitation inherited from test-history v0.1.0 (no native ingest dedup).
  The cleaner long-term fix is adding dedup in test-history itself; out of scope
  here.

## 9. Appendix — quick command reference

```bash
# Plugin lifecycle
hermes plugins list
hermes plugins enable hermes-flaky-detective
hermes plugins disable hermes-flaky-detective

# This plugin's CLI
hermes flaky-detective scan --window 14 --min-fails 3 --format human
hermes flaky-detective status
hermes flaky-detective list --status flaky
hermes flaky-detective install-cron --schedule "0 9 * * *" --deliver local

# Cron (created once; fired by the gateway daemon)
hermes cron create "0 9 * * *" --no-agent --script flaky-scan.sh --deliver local --name flaky-detective
hermes cron list
hermes cron run <job_id>      # trigger on next tick (for testing)
hermes gateway                # the daemon that ticks the scheduler every 60s
```
