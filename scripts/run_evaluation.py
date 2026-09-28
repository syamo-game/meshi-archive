from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.restaurant_extractor import preflight_models
from db.database import SessionLocal, init_db
from db.models import Message
from services.evaluation_service import EvaluationResult, evaluate_imported_message


@dataclass(frozen=True)
class EvaluationArgs:
    concurrency: int
    limit: int | None


def parse_args() -> EvaluationArgs:
    parser = argparse.ArgumentParser(
        description="Evaluate restored messages without overwriting canonical shop data."
    )
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--limit", type=int)
    namespace = parser.parse_args()
    if not 1 <= namespace.concurrency <= 8:
        parser.error("--concurrency must be between 1 and 8")
    if namespace.limit is not None and namespace.limit < 1:
        parser.error("--limit must be at least 1")
    return EvaluationArgs(concurrency=namespace.concurrency, limit=namespace.limit)


async def run_one(message_id: str, semaphore: asyncio.Semaphore) -> EvaluationResult:
    async with semaphore:
        db = SessionLocal()
        try:
            return await evaluate_imported_message(db, message_id)
        finally:
            db.close()


async def run(args: EvaluationArgs) -> tuple[EvaluationResult, ...]:
    await preflight_models()
    db = SessionLocal()
    try:
        query = db.query(Message.message_id).order_by(Message.message_id)
        if args.limit is not None:
            query = query.limit(args.limit)
        message_ids = tuple(message_id for (message_id,) in query.all())
    finally:
        db.close()

    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [run_one(message_id, semaphore) for message_id in message_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failures = [result for result in results if isinstance(result, BaseException)]
    if failures:
        details = "; ".join(
            f"{type(failure).__name__}: {failure}" for failure in failures[:10]
        )
        raise RuntimeError(
            f"Evaluation failed: failed_messages={len(failures)}, errors={details}"
        )
    return tuple(result for result in results if isinstance(result, EvaluationResult))


def main() -> int:
    args = parse_args()
    try:
        init_db()
        results = asyncio.run(run(args))
    except Exception as exc:
        print(
            json.dumps(
                {"status": "failed", "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    payload = {
        "status": "completed",
        "messages": len(results),
        "matched_mentions": sum(result.matched_mentions for result in results),
        "new_mentions": sum(result.new_mentions for result in results),
        "differences": sum(result.differences for result in results),
        "skipped": sum(result.skipped for result in results),
        "results": [asdict(result) for result in results],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
