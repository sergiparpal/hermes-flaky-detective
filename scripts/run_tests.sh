#!/usr/bin/env bash
#
# CI-parity test wrapper for the hermes-flaky-detective plugin.
#
# Modeled on the hermes-test-history wrapper: it unsets credential vars and pins
# TZ/LANG so local runs match CI. The suite needs no live Hermes install
# (registration uses a fake ctx; storage and the reader resolve into a temp
# HERMES_HOME via the `profile_env` fixture / `tmp_path`).
#
# Usage:
#   scripts/run_tests.sh                 # run the whole suite (tests/)
#   scripts/run_tests.sh tests/test_detect.py
#   scripts/run_tests.sh tests/test_query.py::test_dedup_keeps_latest_ingest_of_same_logical_run
set -euo pipefail

# Resolve repo root regardless of where this is invoked from.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Unset credential-bearing env vars so tests can never accidentally reach a real
# backend. `unset` is a no-op for vars that are not set, so this is safe.
unset -v \
  OPENAI_API_KEY ANTHROPIC_API_KEY HERMES_API_KEY GEMINI_API_KEY GOOGLE_API_KEY \
  GROQ_API_KEY MISTRAL_API_KEY OPENROUTER_API_KEY XAI_API_KEY DEEPSEEK_API_KEY \
  TOGETHER_API_KEY FIREWORKS_API_KEY COHERE_API_KEY HF_TOKEN \
  AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AZURE_OPENAI_API_KEY 2>/dev/null || true

# Deterministic locale/timezone for reproducible timestamp handling.
export TZ=UTC
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

# HERMES_HOME is set per-test by the `profile_env` fixture; nothing to export.

exec python3 -m pytest "${@:-tests/}"
