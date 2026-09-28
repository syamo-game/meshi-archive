from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import SessionLocal, init_db
from services.duplicate_service import (
    apply_safe_duplicate_merges,
    build_duplicate_shadow_report,
    build_safe_duplicate_merge_plan,
)


@dataclass(frozen=True)
class DuplicateReportArgs:
    apply_safe: bool


def _parse_args(argv: Sequence[str] | None) -> DuplicateReportArgs:
    parser = argparse.ArgumentParser(
        description="Report duplicate shops and optionally apply safe merges."
    )
    parser.add_argument(
        "--apply-safe",
        action="store_true",
        help="Apply only duplicate merges that satisfy every safe condition.",
    )
    parsed = parser.parse_args(argv)
    return DuplicateReportArgs(apply_safe=bool(parsed.apply_safe))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    db = None
    try:
        init_db()
        db = SessionLocal()
        report = build_duplicate_shadow_report(db)
        safe_plan = build_safe_duplicate_merge_plan(db)
        merge_result = apply_safe_duplicate_merges(db) if args.apply_safe else None
        if args.apply_safe:
            db.commit()
        else:
            db.rollback()
    except Exception as exc:
        if db is not None:
            db.rollback()
        print(
            json.dumps(
                {
                    "status": "failed",
                    "mode": "apply_safe" if args.apply_safe else "dry_run",
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        if db is not None:
            db.close()

    payload = asdict(report)
    payload.update(
        {
            "status": "applied" if args.apply_safe else "reported",
            "mode": "apply_safe" if args.apply_safe else "dry_run",
            "candidate_group_count": len(report.groups),
            "candidate_shop_count": report.candidate_shop_count,
            "auto_merge_group_count": report.auto_merge_group_count,
            "safe_merge_plan": asdict(safe_plan),
            "safe_merge_group_count": safe_plan.safe_group_count,
            "safe_merge_shop_count": safe_plan.safe_shop_count,
        }
    )
    if merge_result is not None:
        payload.update(
            {
                "safe_merge_result": asdict(merge_result),
                "merged_group_count": merge_result.merged_group_count,
                "merged_shop_count": merge_result.merged_shop_count,
                "moved_mention_count": merge_result.moved_mention_count,
            }
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
