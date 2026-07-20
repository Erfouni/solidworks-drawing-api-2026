from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "exports" / "solidworks_drawing_api_2026.sqlite"
MANIFEST = ROOT / "exports" / "solidworks_drawing_api_2026_manifest.json"
QUERY = ROOT / "scripts" / "query_solidworks_drawing_api_db.py"


def run_query(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(QUERY), *arguments],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )


class DatabaseReleaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_database_matches_manifest_and_passes_integrity_checks(self) -> None:
        digest = hashlib.sha256(DB.read_bytes()).hexdigest()
        self.assertEqual(self.manifest["database_sha256"], digest)
        self.assertEqual(DB.stat().st_size, self.manifest["database_bytes"])

        connection = sqlite3.connect(DB.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            self.assertEqual("ok", connection.execute("PRAGMA integrity_check").fetchone()[0])
            self.assertEqual([], connection.execute("PRAGMA foreign_key_check").fetchall())
            self.assertEqual(2405, connection.execute("SELECT COUNT(*) FROM api_members").fetchone()[0])
            self.assertEqual(32, connection.execute("SELECT COUNT(*) FROM workflows").fetchone()[0])
        finally:
            connection.close()

    def test_help_renders_bilingual_example_as_utf8(self) -> None:
        completed = run_query("--help")
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("section view", completed.stdout)
        self.assertIn("نمای برش", completed.stdout)

    def test_symbol_lookup_returns_source_traceable_result(self) -> None:
        completed = run_query("--symbol", "IDrawingDoc::CreateSectionViewAt5")
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(1, len(payload))
        self.assertEqual("CreateSectionViewAt5", payload[0]["api_member"])
        self.assertTrue(payload[0]["official_url"].startswith("https://help.solidworks.com/"))

    def test_persian_full_text_search_returns_results(self) -> None:
        completed = run_query("نمای برش", "--limit", "5")
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertGreaterEqual(len(payload), 1)
        source_urls = [item["source_url"] for item in payload if item["source_url"]]
        self.assertGreaterEqual(len(source_urls), 1)
        self.assertTrue(all(url.startswith("https://help.solidworks.com/") for url in source_urls))

    def test_missing_database_fails_without_creating_a_file(self) -> None:
        missing = ROOT / "tests" / "missing.sqlite"
        self.assertFalse(missing.exists())
        completed = run_query("--db", str(missing), "--coverage")
        self.assertEqual(2, completed.returncode)
        self.assertIn("unable to open database read-only", completed.stderr)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
