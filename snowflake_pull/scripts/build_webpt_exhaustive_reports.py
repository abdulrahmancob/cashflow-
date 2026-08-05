"""Build Inventory → Matrix → Unknown Fields → Coverage scaffolding (offline)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRAPER = ROOT / "webpt_edco_scraper"
for _p in (str(ROOT), str(SCRAPER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from snowflake_pull.webpt_coverage import (  # noqa: E402
    build_coverage_report,
    write_coverage_report,
)
from snowflake_pull.webpt_extractability import (  # noqa: E402
    build_extractability_matrix,
    write_extractability_matrix,
)
from snowflake_pull.webpt_unknown_fields import (  # noqa: E402
    build_unknown_fields_report,
    write_unknown_fields_report,
)

DEFAULT_REPORTS = (
    ROOT / "snowflake_pull" / "artifacts" / "side_by_side_case" / "reports"
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS)
    args = ap.parse_args()

    # 1) inventory from live http log + fixtures
    from snowflake_pull.webpt_inventory import (
        build_inventory_from_http_log,
        write_inventory,
    )

    # Reuse fixture field attachment from build_webpt_inventory
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import build_webpt_inventory as binv  # type: ignore

    http_log = args.reports_dir / "http_requests.jsonl"
    if http_log.is_file():
        inventory = build_inventory_from_http_log(http_log)
        binv._attach_fixture_fields(
            inventory, args.reports_dir / "webpt_probe_raw"
        )
        write_inventory(inventory, args.reports_dir)
        print(f"Inventory endpoints: {inventory.get('endpoint_count')}")
    else:
        print(f"WARN: missing {http_log}")

    inv_path = args.reports_dir / "webpt_inventory.json"

    # 2) matrix
    matrix = build_extractability_matrix(inv_path)
    md = write_extractability_matrix(matrix, args.reports_dir)
    print(f"Wrote {md}")

    # 3) unknown fields
    chart_paths = list((SCRAPER / "output").rglob("patient_chart_*.html"))
    pay_paths = list((SCRAPER / "output").rglob("patient_payments_*.html"))
    pay_paths += list((SCRAPER / "output").rglob("payments_http_*.html"))
    uf = build_unknown_fields_report(
        chart_html_paths=chart_paths,
        payments_html_paths=pay_paths,
        scraper_path=SCRAPER,
    )
    jp, mp = write_unknown_fields_report(uf, args.reports_dir)
    print(f"Wrote {mp}")

    # 4) coverage skeleton (enriched later by run_case_enrich_from_raw)
    cov = build_coverage_report(
        inventory_path=inv_path,
        sample_enrich_rows=None,
        raw_sample_stats={"cases_with_raw": 0, "mean_raw_coverage_pct": 0.0},
    )
    cjp, cmp_ = write_coverage_report(cov, args.reports_dir)
    print(f"Wrote {cmp_} coverage={cov['coverage_pct']}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
