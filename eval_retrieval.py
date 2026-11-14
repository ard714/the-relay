"""
eval_retrieval.py - Evaluate Retrieval Settings (A, B, C) on the 18 curated questions.

Runs each question under:
- Setting A: Small pieces (120 tokens), 3 pieces fetched, no re-ranking
- Setting B: Medium pieces (280 tokens), 5 pieces fetched, no re-ranking
- Setting C: Large pieces (500 tokens), 7 pieces fetched, with TF-IDF cosine re-ranking

Appends all results directly to results.csv (plus traces.jsonl and answers.jsonl)
so compute_tables.py can compute Table 3 strictly and exclusively from results.csv.
"""

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx
from aiohttp import web

from config import RETRIEVAL_SETTINGS
from groq_client import GroqClient
from limiter import SharedLimiter
from load_runner import RESULTS_CSV_COLUMNS, StandinManager, append_results, grade_answer
from pipeline import PipelineResult, answer
from standin import create_app


def load_questions_pool(filepath: str = "questions.json") -> List[Dict[str, Any]]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Questions pool '{filepath}' not found.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


async def evaluate_setting(
    setting_key: str,
    questions: List[Dict[str, Any]],
    client: GroqClient,
    limiter: SharedLimiter,
    source: str = "standin",
    user_patience: float = 15.0,
    csv_path: str = "results.csv",
    traces_path: str = "traces.jsonl",
    answers_path: str = "answers.jsonl",
) -> List[Dict[str, Any]]:
    setting_cfg = RETRIEVAL_SETTINGS[setting_key]
    scenario_name = f"eval_setting_{setting_key.lower()}"

    print(f"\n{'='*90}")
    print(f"EVALUATING RETRIEVAL SETTING {setting_key}: {setting_cfg['name']}")
    print(f"Description: {setting_cfg['description']}")
    print(f"{'='*90}")

    csv_rows = []
    trace_dicts = []
    answer_rows = []

    for i, q_obj in enumerate(questions, 1):
        q_id = q_obj.get("id", f"q{i:02d}")
        question = q_obj["question"]
        req_id = f"eval_{setting_key.lower()}_{i:03d}_{uuid.uuid4().hex[:6]}"
        t_start_iso = datetime.now(timezone.utc).isoformat()
        t_start_perf = time.perf_counter()

        print(f"[{i:02d}/{len(questions)}] ({setting_key}) '{question[:45]}...' ", end="", flush=True)

        try:
            res: PipelineResult = await asyncio.wait_for(
                answer(
                    question=question,
                    deadline=user_patience,
                    setting_key=setting_key,
                    client=client,
                    limiter=limiter,
                ),
                timeout=user_patience + 1.0,
            )
        except asyncio.TimeoutError:
            t_end_perf = time.perf_counter()
            tot_ms = round((t_end_perf - t_start_perf) * 1000, 2)
            res = PipelineResult(
                answer="Request timed out: user patience expired.",
                status="timeout",
                status_code=408,
                trace=None,
                citations=[],
                retrieved_hits=[],
                rounds_used=0,
                degraded_stage=0,
            )
        except Exception as e:
            t_end_perf = time.perf_counter()
            tot_ms = round((t_end_perf - t_start_perf) * 1000, 2)
            res = PipelineResult(
                answer=f"Error executing request: {e}",
                status="error",
                status_code=500,
                trace=None,
                citations=[],
                retrieved_hits=[],
                rounds_used=0,
                degraded_stage=0,
            )

        # Normalize status strictly to: ok, rate_limited, timeout, error
        norm_status = res.status
        if norm_status == "refused_daily_budget_exhausted":
            norm_status = "rate_limited"
        elif norm_status.startswith("refused"):
            norm_status = "ok"
        elif norm_status not in ("ok", "rate_limited", "timeout", "error"):
            norm_status = "error"

        if res.trace:
            res.trace.finalize()
            ttft_ms = res.trace.delay_before_first_token_ms if res.trace.delay_before_first_token_ms > 0 else None
            tot_ms = res.trace.total_ms
            retries = sum(s.retries for s in res.trace.steps)
            tokens_in = res.trace.total_tokens_in
            tokens_out = res.trace.total_tokens_out
            trace_dict = res.trace.to_dict()
        else:
            ttft_ms = None
            retries = 0
            tokens_in = 0
            tokens_out = 0
            trace_dict = {"question": question, "total_ms": tot_ms, "status": norm_status, "steps": []}

        grade = grade_answer(q_obj, res)

        ttft_val = round(ttft_ms, 2) if (ttft_ms is not None and norm_status != "timeout") else ""

        print(f"| Status: {norm_status} | Grade: {grade} | TTFT: {ttft_val} ms | Total: {tot_ms} ms")

        csv_row = {
            "request_id": req_id,
            "scenario": scenario_name,
            "source": source,
            "start_time": t_start_iso,
            "first_token_ms": ttft_val,
            "total_ms": tot_ms,
            "status": norm_status,
            "retries": retries,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "retrieval_setting": setting_key,
            "answer_grade": grade,
        }
        csv_rows.append(csv_row)
        trace_dicts.append(trace_dict)

        answer_rows.append({
            "request_id": req_id,
            "scenario": scenario_name,
            "source": source,
            "start_time": t_start_iso,
            "question_id": q_id,
            "question": question,
            "answer": res.answer,
            "citations": res.citations,
            "status": norm_status,
            "status_code": res.status_code,
            "answer_grade": grade,
            "degraded_stage": res.degraded_stage,
        })

        # Pacing between evaluation questions
        await asyncio.sleep(0.3)

    append_results(
        csv_rows=csv_rows,
        trace_dicts=trace_dicts,
        answer_rows=answer_rows,
        csv_path=csv_path,
        traces_path=traces_path,
        answers_path=answers_path,
    )

    return csv_rows


async def run_all_evaluations(
    questions_path: str = "questions.json",
    csv_path: str = "results.csv",
    traces_path: str = "traces.jsonl",
    answers_path: str = "answers.jsonl",
    source: str = "standin",
    standin_url: str = "http://127.0.0.1:8000",
) -> None:
    questions = load_questions_pool(questions_path)

    standin_mgr = None
    if source == "standin":
        standin_mgr = StandinManager(base_url=standin_url)
        await standin_mgr.ensure_running()
        client = GroqClient(base_url=f"{standin_url}/v1/chat/completions")
    else:
        client = GroqClient()

    # Shared limiter with generous ceiling for evaluation
    limiter = SharedLimiter(max_in_flight=10, rpm_limit=120)

    try:
        for setting in ["A", "B", "C"]:
            if standin_mgr:
                await standin_mgr.reset()
            limiter.reset()
            await evaluate_setting(
                setting_key=setting,
                questions=questions,
                client=client,
                limiter=limiter,
                source=source,
                user_patience=15.0,
                csv_path=csv_path,
                traces_path=traces_path,
                answers_path=answers_path,
            )
            await asyncio.sleep(1.0)
    finally:
        if standin_mgr:
            await standin_mgr.shutdown()
        await client.close()

    print("\n================================================================================")
    print(f"RETRIEVAL SETTING EVALUATION COMPLETE: All rows appended to '{csv_path}'")
    print("================================================================================\n")


def main():
    parser = argparse.ArgumentParser(description="Evaluate retrieval settings A, B, and C.")
    parser.add_argument("--questions", default="questions.json", help="Path to questions JSON file")
    parser.add_argument("--csv", default="results.csv", help="Path to append CSV results")
    parser.add_argument("--traces", default="traces.jsonl", help="Path to append traces")
    parser.add_argument("--answers", default="answers.jsonl", help="Path to append answers")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="Stand-in service base URL")
    parser.add_argument("--real", action="store_true", help="Run against real Groq API (requires --cap)")
    parser.add_argument("--cap", type=int, default=None, help="Safety cap for real run")

    args = parser.parse_args()

    source = "real" if args.real else "standin"
    if args.real:
        if not args.cap or args.cap > 30:
            print("ERROR: Real evaluation requires --cap <= 30.", file=sys.stderr)
            sys.exit(1)

    asyncio.run(
        run_all_evaluations(
            questions_path=args.questions,
            csv_path=args.csv,
            traces_path=args.traces,
            answers_path=args.answers,
            source=source,
            standin_url=args.url,
        )
    )


if __name__ == "__main__":
    main()
