# hermes-flaky-detective
Hermes Agent plugin that detects flaky tests by reading the hermes-test-history SQLite database. Persists verdicts to its own local DB, exposes an is_flaky tool to the agent, and runs a nightly detection sweep as a no-agent cron job (zero LLM cost).
