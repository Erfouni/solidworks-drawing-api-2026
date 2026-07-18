# SOLIDWORKS Drawing API Database 2026

This is a normalized, source-traceable SQLite knowledge base for drawing creation
and automation with SOLIDWORKS macros, scripts, COM add-ins, and Document Manager.

- Database: [`exports/solidworks_drawing_api_2026.sqlite`](exports/solidworks_drawing_api_2026.sqlite)
- SHA-256: `f20e73e5e90f5fcd331fb33ead74fbdbdea10152f49fc39dc3135e64de47f2e5`
- Validation: `True` (`PRAGMA integrity_check = ok`)

[Download the SQLite database](exports/solidworks_drawing_api_2026.sqlite) ·
[View the machine-readable manifest](exports/solidworks_drawing_api_2026_manifest.json)

## Quick start

Python's standard-library SQLite driver is enough for searching the delivered
database:

```powershell
python scripts/query_solidworks_drawing_api_db.py "section view"
python scripts/query_solidworks_drawing_api_db.py "نمای برش"
python scripts/query_solidworks_drawing_api_db.py --symbol IDrawingDoc::CreateSectionViewAt5
python scripts/query_solidworks_drawing_api_db.py --workflow create-section-view
python scripts/query_solidworks_drawing_api_db.py --coverage
```

## Contents

- 192 API types/interfaces/event delegates
- 2,405 methods/properties with VBA/.NET/C# signatures, parameters, returns, remarks, selection/unit notes, availability, and obsolete replacements
- 2,808 normalized parameters
- 432 relevant enumerations and the full command-ID enumeration
- 7,688 enumeration/command values
- 1,293 official relevant examples indexed by objective, preconditions, postconditions, API calls, language, and exact source URL
- 149 official Programming Guide topics
- 32 task workflows with 129 ordered steps
- 4,554 full-text search documents (English and Persian aliases/tags)

The database stores compact structured facts and original workflow synthesis, not a
verbatim offline mirror. Open `official_url` for the complete official page/example.

## Quick search

```sql
SELECT entity_kind, title, snippet(search_fts, 3, '[', ']', ' … ', 24) AS hit, source_url
FROM search_fts
WHERE search_fts MATCH '"section view"'
ORDER BY rank
LIMIT 20;
```

Persian aliases are indexed too:

```sql
SELECT entity_kind, title, snippet(search_fts, 3, '[', ']', ' … ', 24) AS hit
FROM search_fts
WHERE search_fts MATCH '"نمای برش" OR section'
ORDER BY rank
LIMIT 20;
```

Find the recommended workflow first, then resolve its exact calls:

```sql
SELECT * FROM v_workflow_api_map WHERE slug='create-section-view';

SELECT * FROM v_api_member_details
WHERE api_type='IDrawingDoc' AND api_member='CreateSectionViewAt5';
```

Find non-obsolete methods for a task:

```sql
SELECT api_type, api_member, summary, signature_vba, selection_requirements,
       unit_notes, availability, official_url
FROM v_api_member_details
WHERE is_obsolete=0 AND drawing_tasks LIKE '%views%'
ORDER BY api_type, api_member;
```

## Important scope rules

- Every member of drawing-specific interfaces is included.
- Broad interfaces such as `ISldWorks`, `IModelDoc2`, and `IModelDocExtension` are
  limited to members needed by drawing workflows.
- Every enum referenced by those members is included, plus drawing-keyword enums
  and all `swCommands_e` values.
- Full official example source is accessed through its `official_url`; the database
  keeps searchable behavior, requirements, effects, and API-call inventories.
- API geometry/placement values are often SI (meters/radians). Always use the exact
  member parameter descriptions and `unit_notes`.

## Rebuilding

`scripts/build_solidworks_drawing_api_db.py` is the reproducible builder used for
this artifact. It requires Python 3, `lxml`, a licensed local SOLIDWORKS 2026 help
installation, and extracted copies of the official CHM sources under `work/`:
`sa` (`sldworksapi`), `svb` (`sldworksapivb6`), `sc` (`swconst`), `spg`
(`sldworksapiprogguide`), `scmd` (`swcommands`), and `sdm` (`swdocmgrapi`). The
extracted source files are intentionally excluded from this repository. The builder
maps every included record back to its official `help.solidworks.com` URL.

## Provenance and disclaimer

This is an independent, unofficial search index. It is not affiliated with or
endorsed by Dassault Systèmes. SOLIDWORKS is a trademark of Dassault Systèmes or
its subsidiaries. The repository does not redistribute the original CHM files or
a verbatim documentation mirror; it stores normalized facts, compact summaries,
search metadata, and source links. Consult the linked official documentation and
your SOLIDWORKS license terms for authoritative usage requirements.
