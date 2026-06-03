#!/usr/bin/env bash
#
# Nightly no-agent cron shim for hermes-flaky-detective.
#
# Installed into ~/.hermes/scripts/ by `hermes flaky-detective install-cron`.
# A no-agent cron job runs this on a schedule and delivers its stdout verbatim at
# zero LLM cost. `--format cron` prints the flaky-test changes (or nothing, for a
# silent tick on quiet nights). A non-zero exit makes Hermes deliver an error
# alert, so `set -euo pipefail` ensures a broken sweep cannot fail silently.
set -euo pipefail
exec hermes flaky-detective scan --format cron
