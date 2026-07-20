# Security policy

## Reporting

Use GitHub private vulnerability reporting when available. Do not open a public
issue for a vulnerability involving path handling, unexpected database writes,
network fetching, credential exposure, or malicious SQLite content.

## Trust boundary

The checked-in query CLI is intentionally read-only and unauthenticated. The
database builder has a broader trust boundary because it reads source files and
can fetch official help pages. Never run the builder against untrusted HTML or an
untrusted output path without reviewing the inputs.

No SOLIDWORKS license keys, API tokens, customer files, or private engineering
artifacts should be stored in this repository or its issue tracker.
