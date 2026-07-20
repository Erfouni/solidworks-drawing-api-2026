# Contributing

Contributions should make the database easier to verify, search, or use without
turning it into an unofficial copy of SOLIDWORKS documentation.

## Good contributions

- reproducible query or artifact-validation tests;
- corrections backed by an exact official `help.solidworks.com` URL;
- new task workflows synthesized from source-traceable API facts;
- better English or Persian search aliases;
- safer read-only integrations for RAG, MCP, macros, or engineering tools.

Do not commit licensed CHM files, copied documentation pages, credentials,
customer drawings, proprietary macros, or generated databases whose provenance
cannot be explained.

## Development workflow

1. Create a focused branch from `main`.
2. Add or update tests for the behavior you change.
3. Run:

   ```powershell
   python -m unittest discover -s tests -v
   python -m compileall -q scripts tests
   git diff --check
   ```

4. In the pull request, describe the evidence source, validation performed, and
   whether the SQLite artifact or manifest changed.

Rebuilding the complete database requires the sources and dependencies described
in [README.md](README.md). Query and release-artifact tests require only Python's
standard library and the checked-in database.
