"""The pre-deploy scan finds names that will not resolve, including lazy and keyword uses."""

from __future__ import annotations

import textwrap
from pathlib import Path

from cashflow_ops import deploy_preflight as dp

ROOT = Path(__file__).resolve().parents[2]


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    for package in ("cashflow_db", "cashflow_forecast"):
        init = tmp_path / package / "__init__.py"
        if (tmp_path / package).is_dir() and not init.exists():
            init.write_text("", encoding="utf-8")
    return tmp_path


def _kinds(root: Path) -> list[tuple[str, str]]:
    return [(issue.kind, issue.detail) for issue in dp.static_issues(root)]


def test_current_tree_has_no_unresolved_names():
    assert dp.static_issues(ROOT) == []
    assert dp.compile_issues(ROOT) == []


def test_lazy_import_of_a_removed_name_is_caught(tmp_path):
    root = _tree(tmp_path, {
        "cashflow_db/visits.py": "KEEP = 1\n",
        "cashflow_forecast/run.py": """
            def go():
                try:
                    from cashflow_db.visits import GONE
                except Exception:
                    return None
                return GONE
        """,
    })
    assert ("missing", "from cashflow_db.visits import GONE") in _kinds(root)


def test_import_meant_to_be_optional_is_not_flagged(tmp_path):
    root = _tree(tmp_path, {
        "cashflow_db/visits.py": "KEEP = 1\n",
        "cashflow_forecast/run.py": """
            try:
                from cashflow_db.visits import GONE
            except ImportError:
                GONE = None
        """,
    })
    assert _kinds(root) == []


def test_missing_module_and_relative_imports(tmp_path):
    root = _tree(tmp_path, {
        "cashflow_db/sub/__init__.py": "",
        "cashflow_db/sub/a.py": "from .b import THING\nfrom . import c\n",
        "cashflow_db/sub/b.py": "THING = 1\n",
        "cashflow_forecast/run.py": "from cashflow_db.nowhere import x\n",
    })
    found = _kinds(root)
    assert ("nomodule", "from cashflow_db.sub.c") not in found
    assert ("nomodule", "from cashflow_db.nowhere") in found
    assert not any(kind == "missing" and "THING" in detail for kind, detail in found)


def test_attribute_on_an_imported_module(tmp_path):
    root = _tree(tmp_path, {
        "cashflow_db/payments.py": "def get_lines(conn):\n    return []\n",
        "cashflow_forecast/run.py": """
            from cashflow_db import payments as pay_repo

            def go(conn):
                pay_repo.get_lines(conn)
                return pay_repo.get_slim(conn)
        """,
    })
    assert _kinds(root) == [("attr", "cashflow_db.payments.get_slim")]


def test_local_variable_with_the_alias_name_is_not_checked(tmp_path):
    root = _tree(tmp_path, {
        "cashflow_db/payments.py": "def get_lines(conn):\n    return []\n",
        "cashflow_forecast/run.py": """
            from cashflow_db import payments

            def go(payments):
                return payments.anything
        """,
    })
    assert _kinds(root) == []


def test_unknown_keyword_is_caught_unless_kwargs_or_decorated(tmp_path):
    root = _tree(tmp_path, {
        "cashflow_db/reconciliation.py": """
            import functools

            def get_lines(conn, *, run_id=None):
                return []

            def flexible(conn, **options):
                return options

            @functools.lru_cache
            def cached(conn):
                return 1
        """,
        "cashflow_forecast/run.py": """
            from cashflow_db import reconciliation as recon_repo
            from cashflow_db.reconciliation import get_lines

            def go(conn):
                recon_repo.get_lines(conn, run_id=1, as_of=2)
                get_lines(conn, as_of=3)
                recon_repo.flexible(conn, as_of=4)
                recon_repo.cached(conn, as_of=5)
        """,
    })
    found = _kinds(root)
    assert found.count(("kwarg", "cashflow_db.reconciliation.get_lines(as_of=)")) == 2
    assert not any("flexible" in detail or "cached" in detail for _kind, detail in found)


def test_syntax_errors_are_reported_without_writing_bytecode(tmp_path):
    root = _tree(tmp_path, {"cashflow_db/broken.py": "def x(:\n"})
    assert [issue.kind for issue in dp.compile_issues(root)] == ["syntax"]
    assert not list(root.rglob("__pycache__"))


def test_known_issues_are_ignored_and_new_ones_fail(tmp_path):
    root = _tree(tmp_path, {
        "cashflow_db/visits.py": "KEEP = 1\n",
        "cashflow_forecast/run.py": "from cashflow_db.visits import OLD\nfrom cashflow_db.visits import NEW\n",
    })
    issues = dp.static_issues(root)
    known = {issue.key for issue in issues if "OLD" in issue.detail}
    fresh = dp.new_issues(issues, known)
    assert [issue.detail for issue in fresh] == ["from cashflow_db.visits import NEW"]
    known_file = tmp_path / "known.txt"
    assert dp.main(["--root", str(root), "--static-only", "--write-known", str(known_file)]) == 0
    assert dp.main(["--root", str(root), "--static-only", "--known", str(known_file)]) == 0


def test_skips_virtualenvs_and_tests(tmp_path):
    root = _tree(tmp_path, {
        "cashflow_db/visits.py": "KEEP = 1\n",
        "cashflow_db/venv/lib/x.py": "from cashflow_db.visits import GONE\n",
        "cashflow_db/tests/test_x.py": "from cashflow_db.visits import GONE\n",
    })
    assert _kinds(root) == []
