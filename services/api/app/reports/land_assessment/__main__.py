# -*- coding: utf-8 -*-
"""CLI: python -m app.reports.land_assessment --land-id <id> [--out path]

Also supports offline fixtures:
  python -m app.reports.land_assessment --from-dir /path/to/openfarm-report-hebei --out report.pdf
"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate 选地体检（白话版）PDF")
    parser.add_argument("--land-id", help="canonical agric_satellite land_id")
    parser.add_argument(
        "--from-dir", help="Fixture directory (field.json + all_indices.csv)"
    )
    parser.add_argument("--out", help="Output PDF path")
    parser.add_argument(
        "--json-summary", action="store_true", help="Print scorecard summary JSON"
    )
    args = parser.parse_args(argv)

    if not args.land_id and not args.from_dir:
        parser.error("Provide --land-id or --from-dir")

    from app.reports.land_assessment.service import generate_assessment_pdf

    if args.from_dir:
        result = generate_assessment_pdf(data_dir=args.from_dir, out_path=args.out)
    else:
        from app.core.database_sync import SyncSession

        session = SyncSession()
        try:
            result = generate_assessment_pdf(
                session=session, land_id=args.land_id, out_path=args.out
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    print(f"WROTE {result['out_path']}")
    print(
        f"SCORE {result['score']} {result['grade']} {result['light']} "
        f"src={result['indices_source']} rows={result['n_index_rows']}"
    )
    if args.json_summary:
        summary = {
            "score": result["score"],
            "grade": result["grade"],
            "light": result["light"],
            "one_liner": result["one_liner"],
            "dimensions": result["scorecard"]["dimensions"],
            "flood_evidence": result.get("flood_evidence"),
            "open_water_dates": (result.get("rs") or {}).get("open_water_dates"),
            "out_path": result["out_path"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
