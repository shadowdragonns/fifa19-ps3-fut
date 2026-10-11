Please describe the affected FUT menu or match stage, game title/update and
RPCS3 revision. Share sanitized findings; keep credentials, saves and captures
private. Do not submit game binaries or extracted media.

Run the tests and publication audit in docs/DEVELOPMENT.md before submitting
a pull request. Add regression coverage for protocol/state changes, preserve
per-user ownership, atomic inventory writes and retry-safe match receipts.
Use domain hooks for extensions rather than altering raw transport replies.
