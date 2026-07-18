#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Query the generated SOLIDWORKS drawing API SQLite knowledge base."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO / "exports" / "solidworks_drawing_api_2026.sqlite"


def rows_as_dicts(cursor):
    return [dict(row) for row in cursor.fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="*", help="FTS5 query, for example: section view or نمای برش")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--kind", help="Filter entity_kind (workflow, api_member, example, api_enum, guide_topic)")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--symbol", help="Exact symbol such as IDrawingDoc::CreateSectionViewAt4")
    parser.add_argument("--workflow", help="Exact workflow slug")
    parser.add_argument("--coverage", action="store_true")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        if args.coverage:
            result = rows_as_dicts(conn.execute("SELECT * FROM coverage ORDER BY area"))
        elif args.symbol:
            if "::" in args.symbol:
                type_name, member_name = args.symbol.split("::", 1)
                result = rows_as_dicts(conn.execute(
                    "SELECT * FROM v_api_member_details WHERE api_type=? AND api_member=?",
                    (type_name, member_name),
                ))
            else:
                result = rows_as_dicts(conn.execute("SELECT * FROM api_types WHERE name=?", (args.symbol,)))
        elif args.workflow:
            result = rows_as_dicts(conn.execute(
                "SELECT * FROM v_workflow_api_map WHERE slug=? ORDER BY sequence",
                (args.workflow,),
            ))
        elif args.query:
            query = " ".join(args.query)
            sql = (
                "SELECT entity_kind,entity_id,title,"
                "snippet(search_fts,3,'[',']',' … ',28) AS hit,source_url,rank "
                "FROM search_fts WHERE search_fts MATCH ?"
            )
            values: list[object] = [query]
            if args.kind:
                sql += " AND entity_kind=?"
                values.append(args.kind)
            sql += " ORDER BY rank LIMIT ?"
            values.append(max(1, min(args.limit, 200)))
            result = rows_as_dicts(conn.execute(sql, values))
        else:
            parser.error("provide a query or use --symbol, --workflow, or --coverage")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
