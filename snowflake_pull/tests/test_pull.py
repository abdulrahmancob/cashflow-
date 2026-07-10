"""Unit tests for SQL resolution and CSV writing (no live Snowflake)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from snowflake_pull.pull import resolve_sql, write_csv


class ResolveSqlTests(unittest.TestCase):
    def test_prefers_explicit_sql(self) -> None:
        self.assertEqual(
            resolve_sql(sql="SELECT 1", sql_file=None, default_sql="SELECT 0"),
            "SELECT 1",
        )

    def test_reads_sql_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "q.sql"
            path.write_text("SELECT 42 AS n\n", encoding="utf-8")
            self.assertEqual(
                resolve_sql(sql=None, sql_file=path, default_sql="SELECT 0"),
                "SELECT 42 AS n",
            )

    def test_rejects_both(self) -> None:
        with self.assertRaises(SystemExit):
            resolve_sql(sql="SELECT 1", sql_file=Path("x.sql"), default_sql="SELECT 0")

    def test_falls_back_to_default(self) -> None:
        self.assertEqual(
            resolve_sql(sql=None, sql_file=None, default_sql="SELECT 9"),
            "SELECT 9",
        )


class WriteCsvTests(unittest.TestCase):
    def test_writes_header_and_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            n = write_csv(path, ["a", "b"], [(1, "x"), (2, "y")])
            self.assertEqual(n, 2)
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.splitlines(), ["a,b", "1,x", "2,y"])


if __name__ == "__main__":
    unittest.main()
