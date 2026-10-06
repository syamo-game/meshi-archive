from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import case, func

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import DATABASE_URL, SessionLocal, init_db
from db.models import Message, ProcessingRun, ProcessingStatus


@dataclass(frozen=True)
class ReportArgs:
    baseline_calls: int | None
    baseline_cost_microusd: int | None
    assert_targets: bool


@dataclass(frozen=True)
class StageMetrics:
    stage: str
    runs: int
    model_calls: int
    web_search_calls: int
    input_tokens: int
    output_tokens: int
    estimated_cost_microusd: int
    failed_runs: int


@dataclass(frozen=True)
class ProcessingReport:
    database_url: str
    processed_messages: int
    model_calls: int
    web_search_calls: int
    billed_calls: int
    estimated_cost_microusd: int
    call_reduction_percent: float | None
    cost_reduction_percent: float | None
    stages: tuple[StageMetrics, ...]


def parse_args() -> ReportArgs:
    parser = argparse.ArgumentParser(description="Report pipeline calls, cost, and acceptance targets.")
    parser.add_argument("--baseline-calls", type=int)
    parser.add_argument("--baseline-cost-microusd", type=int)
    parser.add_argument("--assert-targets", action="store_true")
    namespace = parser.parse_args()
    for name in ("baseline_calls", "baseline_cost_microusd"):
        value = getattr(namespace, name)
        if value is not None and value <= 0:
            parser.error(f"--{name.replace('_', '-')} must be greater than zero")
    if namespace.assert_targets and (
        namespace.baseline_calls is None or namespace.baseline_cost_microusd is None
    ):
        parser.error("--assert-targets requires both baseline values")
    return ReportArgs(
        baseline_calls=namespace.baseline_calls,
        baseline_cost_microusd=namespace.baseline_cost_microusd,
        assert_targets=namespace.assert_targets,
    )


def reduction_percent(current: int, baseline: int | None) -> float | None:
    if baseline is None:
        return None
    return round((baseline - current) / baseline * 100, 2)


def build_report(args: ReportArgs) -> ProcessingReport:
    init_db()
    db = SessionLocal()
    try:
        rows = (
            db.query(
                ProcessingRun.stage,
                func.count(ProcessingRun.id),
                func.sum(case((ProcessingRun.model.isnot(None), 1), else_=0)),
                func.sum(ProcessingRun.web_search_calls),
                func.sum(ProcessingRun.input_tokens),
                func.sum(ProcessingRun.output_tokens),
                func.sum(ProcessingRun.estimated_cost_microusd),
                func.sum(
                    case(
                        (ProcessingRun.status == ProcessingStatus.FAILED.value, 1),
                        else_=0,
                    )
                ),
            )
            .group_by(ProcessingRun.stage)
            .order_by(ProcessingRun.stage)
            .all()
        )
        stages = tuple(
            StageMetrics(
                stage=str(row[0]),
                runs=int(row[1] or 0),
                model_calls=int(row[2] or 0),
                web_search_calls=int(row[3] or 0),
                input_tokens=int(row[4] or 0),
                output_tokens=int(row[5] or 0),
                estimated_cost_microusd=int(row[6] or 0),
                failed_runs=int(row[7] or 0),
            )
            for row in rows
        )
        model_calls = sum(stage.model_calls for stage in stages)
        web_search_calls = sum(stage.web_search_calls for stage in stages)
        estimated_cost = sum(stage.estimated_cost_microusd for stage in stages)
        processed_messages = (
            db.query(func.count(Message.message_id))
            .filter(
                Message.processing_status.in_(
                    [ProcessingStatus.SUCCEEDED.value, ProcessingStatus.IGNORED.value]
                )
            )
            .scalar()
            or 0
        )
        return ProcessingReport(
            database_url=DATABASE_URL,
            processed_messages=int(processed_messages),
            model_calls=model_calls,
            web_search_calls=web_search_calls,
            billed_calls=model_calls + web_search_calls,
            estimated_cost_microusd=estimated_cost,
            call_reduction_percent=reduction_percent(
                model_calls + web_search_calls, args.baseline_calls
            ),
            cost_reduction_percent=reduction_percent(
                estimated_cost, args.baseline_cost_microusd
            ),
            stages=stages,
        )
    finally:
        db.close()


def main() -> int:
    args = parse_args()
    try:
        report = build_report(args)
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1

    payload = asdict(report)
    payload["status"] = "reported"
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not args.assert_targets:
        return 0
    failures: list[str] = []
    if report.processed_messages != 260:
        failures.append(f"processed_messages={report.processed_messages}, expected=260")
    if (report.call_reduction_percent or 0.0) < 40.0:
        failures.append(
            f"call_reduction_percent={report.call_reduction_percent}, expected>=40"
        )
    if (report.cost_reduction_percent or 0.0) < 30.0:
        failures.append(
            f"cost_reduction_percent={report.cost_reduction_percent}, expected>=30"
        )
    if failures:
        print(json.dumps({"status": "target_failed", "errors": failures}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
