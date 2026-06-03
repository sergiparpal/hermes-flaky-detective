"""Pytest bootstrap + shared fixtures for the hermes-flaky-detective plugin.

Hermes loads a plugin by importing its *directory* as a package: the directory
name keeps its hyphen and is loaded via ``importlib.import_module(dir_name)``,
and the plugin's modules use relative imports. We replicate that load here so the
test suite exercises the package exactly as Hermes would, then alias it under a
valid Python identifier (``hermes_flaky_detective``) so test modules can write
``from hermes_flaky_detective import detect``.

Crucially, the alias reuses the *same* module objects, so a ``Storage`` (and the
conn-taking SQL helpers) behave identically whether reached through the plugin's
internal relative imports or the tests' aliased imports.
"""

import importlib
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Bootstrap: load the plugin package the way Hermes does, then alias it.
# --------------------------------------------------------------------------

_PLUGIN_DIR = Path(__file__).resolve().parent.parent          # repo root
_PARENT = str(_PLUGIN_DIR.parent)
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)

# Make the tests' own helper module (``fixtures.py``) importable as a top-level
# module under pytest's importlib mode, which (unlike prepend mode) does not add
# the test file's directory to sys.path automatically.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

_REAL = _PLUGIN_DIR.name                                       # "hermes-flaky-detective"
_ALIAS = "hermes_flaky_detective"

_pkg = importlib.import_module(_REAL)
sys.modules.setdefault(_ALIAS, _pkg)
for _name in ("domain", "timeutil", "detect", "schema", "storage", "query",
              "config", "reporting", "cli"):
    _mod = importlib.import_module(f"{_REAL}.{_name}")
    sys.modules.setdefault(f"{_ALIAS}.{_name}", _mod)

from hermes_flaky_detective import storage  # noqa: E402  (after bootstrap)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def profile_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME under a tmp dir so the profile-aware home is a sandbox.

    Monkeypatch ``Path.home`` and ``HERMES_HOME`` so the plugin's profile-aware
    home resolves inside the tmp dir, never the real one. No singleton to reset:
    each test gets a fresh home (hence a fresh verdicts DB), and every ``Storage``
    owns and closes its own connection.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    yield home


@pytest.fixture
def verdicts_db(profile_env):
    """An open verdicts-DB connection (schema applied); closed on teardown.

    Yields the raw connection so the conn-taking storage helpers can be exercised
    directly; the owning ``Storage`` closes it when the test finishes.
    """
    store = storage.Storage()
    yield store.conn
    store.close()
