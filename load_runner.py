"""
load_runner.py - Workload runner driving the Q&A pipeline across 5 production scenarios.

Scenarios:
1. steady: Poisson arrivals at 15 RPM below provider limits.
2. burst: 20 concurrent requests arriving simultaneously.
3. sustained_overload: Poisson arrivals at 60 RPM (2x provider limits).
4. rpm_halved: Provider limit halved to 15 RPM under steady traffic.
5. daily_budget_exhausted: Daily API quota exhausted (3pm scenario).

Features:
- Config-driven via scenarios.yaml (arrival rate, duration, burst size, user patience).
- Strict user patience enforcement (status=timeout when patience expires).
- Safety guard: Real runs require explicit --real flag and a hard cap (--cap <= 30).
- Logs results to results.csv, traces.jsonl, and answers.jsonl.
- Automatically handles stand-in server lifecycle and dynamic admin configuration.
"""

import argparse
import asyncio
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web
import httpx
import yaml

from config import RETRIEVAL_SETTINGS
from groq_client import GroqClient
from limiter import SharedLimiter
from pipeline import PipelineResult, answer
from standin import create_app

RESULTS_CSV_COLUMNS = [
    "request_id",
    "scenario",
    "source",
    "start_time",
    "first_token_ms",
    "total_ms",
    "status",
    "retries",
    "tokens_in",
    "tokens_out",
    "retrieval_setting",
    "answer_grade",
]


def load_yaml_config(filepath: str = "scenarios.yaml") -> Dict[str, Any]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file '{filepath}' not found.")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_questions_pool(filepath: str = "questions.json") -> List[Dict[str, Any]]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Questions pool '{filepath}' not found.")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def grade_answer(q_obj: Dict[str, Any], res: PipelineResult) -> str:
    """
    Deterministically grade the answer based on ground truth, citations, and pipeline outcome.
    Possible grades: correct, wrong, refused_correctly, refused_wrongly, timeout, rate_limited, budget_exhausted, degraded_correct
    """
    status = res.status
    if status == "timeout":
        return "timeout"
    if status in ("rate_limited",) or res.status_code == 429:
        if "daily" in status or "budget" in status or "daily" in res.answer.lower():
            return "budget_exhausted"
        return "rate_limited"
    if status == "refused_daily_budget_exhausted":
        return "budget_exhausted"

    is_answerable = q_obj.get("answerable", True)

    if not is_answerable:
        # Expected to refuse
        if (
            status.startswith("refused")
            or "papers do not say" in res.answer.lower()
            or "outside that scope" in res.answer.lower()
            or "do not state" in res.answer.lower()
        ):
            return "refused_correctly"
        else:
            return "wrong"  # Hallucinated on an unanswerable question

    # Question is answerable
    if status.startswith("refused"):
        return "refused_wrongly"

    # Check for presence of expected keywords and citations
    expected = q_obj.get("expected_keywords", [])
    ans_lower = res.answer.lower()
    matched = sum(1 for kw in expected if kw.lower() in ans_lower)
    has_citations = len(res.citations) > 0

    if matched >= 1 and has_citations:
        if res.degraded_stage > 0:
            return "degraded_correct"
        return "correct"
    elif matched >= 1:
        return "degraded_correct" if res.degraded_stage > 0 else "correct"
    else:
        return "wrong"


class StandinManager:
    """Manages the lifecycle and admin control of the local standin server."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url.rstrip("/")
        self.runner: Optional[web.AppRunner] = None
        self.site: Optional[web.TCPSite] = None
        self.started_internally: bool = False

    async def ensure_running(self) -> None:
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(f"{self.base_url}/admin/config", timeout=1.0)
                if r.status_code == 200:
                    return
            except Exception:
                pass

        # Start internally
        print(f"Stand-in server not running at {self.base_url}; starting in-process aiohttp runner...")
        app = create_app()
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        import urllib.parse
        parsed = urllib.parse.urlparse(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 8000
        self.site = web.TCPSite(self.runner, host, port)
        await self.site.start()
        self.started_internally = True
        await asyncio.sleep(0.2)

    async def reset(self) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(f"{self.base_url}/admin/reset", timeout=2.0)

    async def apply_admin_overrides(self, admin_config: Dict[str, Any]) -> None:
        async with httpx.AsyncClient() as client:
            if admin_config.get("exhaust_day"):
                await client.post(f"{self.base_url}/admin/exhaust_day", timeout=2.0)
            knobs = {k: v for k, v in admin_config.items() if k != "exhaust_day"}
            if knobs:
                await client.post(f"{self.base_url}/admin/config", json=knobs, timeout=2.0)

    async def shutdown(self) -> None:
        if self.started_internally and self.runner:
            await self.runner.cleanup()


async def execute_single_request(
    request_id: str,
    scenario_name: str,
    source: str,
    question_obj: Dict[str, Any],
    retrieval_setting: str,
    user_patience: float,
    client: GroqClient,
    limiter: SharedLimiter,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Execute a single pipeline request, enforcing user patience deadline and collecting logs."""
    t_start_iso = datetime.now(timezone.utc).isoformat()
    t_start_perf = time.perf_counter()
    question = question_obj["question"]

    try:
        res: PipelineResult = await asyncio.wait_for(
            answer(
                question=question,
                deadline=user_patience,
                setting_key=retrieval_setting,
                client=client,
                limiter=limiter,
            ),
            timeout=user_patience + 0.5,
        )
    except asyncio.TimeoutError:
        t_end_perf = time.perf_counter()
        tot_ms = round((t_end_perf - t_start_perf) * 1000, 2)
        res = PipelineResult(
            answer="Request timed out: user patience expired before completion.",
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

    # Extract metrics from trace
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
        trace_dict = {
            "question": question,
            "total_ms": tot_ms,
            "status": res.status,
            "steps": [],
        }

    grade = grade_answer(question_obj, res)

    # If status is timeout, ttft_ms is empty
    if res.status == "timeout":
        ttft_val: Any = ""
    else:
        ttft_val = round(ttft_ms, 2) if ttft_ms is not None else ""

    csv_row = {
        "request_id": request_id,
        "scenario": scenario_name,
        "source": source,
        "start_time": t_start_iso,
        "first_token_ms": ttft_val,
        "total_ms": tot_ms,
        "status": res.status,
        "retries": retries,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "retrieval_setting": retrieval_setting,
        "answer_grade": grade,
    }

    answer_row = {
        "request_id": request_id,
        "scenario": scenario_name,
        "source": source,
        "start_time": t_start_iso,
        "question_id": question_obj.get("id"),
        "question": question,
        "answer": res.answer,
        "citations": res.citations,
        "status": res.status,
        "status_code": res.status_code,
        "answer_grade": grade,
        "degraded_stage": res.degraded_stage,
    }

    return csv_row, trace_dict, answer_row


def append_results(
    csv_rows: List[Dict[str, Any]],
    trace_dicts: List[Dict[str, Any]],
    answer_rows: List[Dict[str, Any]],
    csv_path: str = "results.csv",
    traces_path: str = "traces.jsonl",
    answers_path: str = "answers.jsonl",
) -> None:
    csv_file = Path(csv_path)
    file_exists = csv_file.exists() and csv_file.stat().st_size > 0

    with open(csv_file, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RESULTS_CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(csv_rows)

    with open(traces_path, "a", encoding="utf-8") as f:
        for t in trace_dicts:
            f.write(json.dumps(t) + "\n")

    with open(answers_path, "a", encoding="utf-8") as f:
        for a in answer_rows:
            f.write(json.dumps(a) + "\n")


async def run_scenario(
    scenario_name: str,
    scenario_cfg: Dict[str, Any],
    source: str,
    questions: List[Dict[str, Any]],
    client: GroqClient,
    limiter: SharedLimiter,
    standin_mgr: Optional[StandinManager],
    csv_path: str = "results.csv",
    traces_path: str = "traces.jsonl",
    answers_path: str = "answers.jsonl",
    max_requests_cap: Optional[int] = None,
    scale_factor: float = 1.0,
) -> List[Dict[str, Any]]:
    print(f"\n{'='*80}")
    print(f"RUNNING SCENARIO: [{scenario_name.upper()}] (Source: {source})")
    print(f"Description: {scenario_cfg.get('description', '')}")
    print(f"{'='*80}")

    arrival_mode = scenario_cfg.get("arrival_mode", "poisson")
    patience = float(scenario_cfg.get("user_patience_sec", 10.0))
    setting = scenario_cfg.get("retrieval_setting", "B")

    # Reset & configure standin if running against standin
    if source == "standin" and standin_mgr:
        await standin_mgr.reset()
        limiter.reset()
        if "standin_admin" in scenario_cfg:
            print(f"Applying stand-in admin overrides: {scenario_cfg['standin_admin']}")
            await standin_mgr.apply_admin_overrides(scenario_cfg["standin_admin"])
            # If scenario halved RPM, update local limiter too
            if "rpm_limit" in scenario_cfg["standin_admin"]:
                limiter.rpm_limit = scenario_cfg["standin_admin"]["rpm_limit"]
            if scenario_cfg["standin_admin"].get("exhaust_day"):
                limiter.set_daily_exhausted(True)

    tasks = []
    q_index = 0

    if arrival_mode == "burst":
        burst_size = int(scenario_cfg.get("burst_size", 20) * scale_factor)
        if max_requests_cap:
            burst_size = min(burst_size, max_requests_cap)

        print(f"Dispatching burst of {burst_size} concurrent requests...")
        for i in range(burst_size):
            req_id = f"req_{scenario_name}_{i+1:03d}_{uuid.uuid4().hex[:6]}"
            q_obj = questions[q_index % len(questions)]
            q_index += 1
            t = asyncio.create_task(
                execute_single_request(
                    request_id=req_id,
                    scenario_name=scenario_name,
                    source=source,
                    question_obj=q_obj,
                    retrieval_setting=setting,
                    user_patience=patience,
                    client=client,
                    limiter=limiter,
                )
            )
            tasks.append(t)

    elif arrival_mode == "poisson":
        rpm = float(scenario_cfg.get("arrival_rate_rpm", 15.0))
        duration = float(scenario_cfg.get("duration_sec", 60.0)) * scale_factor
        lambd = rpm / 60.0  # arrivals per second
        print(f"Generating Poisson arrivals at {rpm:.1f} RPM for {duration:.1f} seconds (lambda={lambd:.3f}/s)...")

        t_sim_start = time.perf_counter()
        req_count = 0

        while (time.perf_counter() - t_sim_start) < duration:
            if max_requests_cap and req_count >= max_requests_cap:
                print(f"Reached request cap of {max_requests_cap}. Halting arrivals.")
                break

            # Exponential inter-arrival time
            inter_arrival = random.expovariate(lambd)
            await asyncio.sleep(inter_arrival)

            if (time.perf_counter() - t_sim_start) >= duration:
                break

            req_count += 1
            req_id = f"req_{scenario_name}_{req_count:03d}_{uuid.uuid4().hex[:6]}"
            q_obj = questions[q_index % len(questions)]
            q_index += 1

            t = asyncio.create_task(
                execute_single_request(
                    request_id=req_id,
                    scenario_name=scenario_name,
                    source=source,
                    question_obj=q_obj,
                    retrieval_setting=setting,
                    user_patience=patience,
                    client=client,
                    limiter=limiter,
                )
            )
            tasks.append(t)

    print(f"Total requests scheduled: {len(tasks)}. Awaiting completion...")
    raw_results = await asyncio.gather(*tasks)

    csv_rows = [r[0] for r in raw_results]
    trace_dicts = [r[1] for r in raw_results]
    answer_rows = [r[2] for r in raw_results]

    append_results(
        csv_rows=csv_rows,
        trace_dicts=trace_dicts,
        answer_rows=answer_rows,
        csv_path=csv_path,
        traces_path=traces_path,
        answers_path=answers_path,
    )

    # Print summary table
    print(f"\n--- Scenario Summary: [{scenario_name}] ---")
    status_counts = {}
    grade_counts = {}
    valid_totals = []
    valid_ttfts = []

    for r in csv_rows:
        st = r["status"]
        gr = r["answer_grade"]
        status_counts[st] = status_counts.get(st, 0) + 1
        grade_counts[gr] = grade_counts.get(gr, 0) + 1
        if r["total_ms"]:
            valid_totals.append(r["total_ms"])
        if r["first_token_ms"] != "":
            valid_ttfts.append(float(r["first_token_ms"]))

    print(f"Requests Processed:  {len(csv_rows)}")
    print(f"Status Breakdown:    {status_counts}")
    print(f"Grade Breakdown:     {grade_counts}")
    if valid_ttfts:
        print(f"TTFT Mean / Median:  {sum(valid_ttfts)/len(valid_ttfts):.1f} ms / {sorted(valid_ttfts)[len(valid_ttfts)//2]:.1f} ms")
    if valid_totals:
        print(f"Total Latency Mean:  {sum(valid_totals)/len(valid_totals):.1f} ms")

    return csv_rows


async def run_load_suite(
    scenario_names: List[str],
    scenarios_cfg: Dict[str, Any],
    source: str,
    questions_file: str,
    csv_path: str,
    traces_path: str,
    answers_path: str,
    cap: Optional[int] = None,
    scale_factor: float = 1.0,
) -> None:
    questions = load_questions_pool(questions_file)
    standin_url = scenarios_cfg.get("defaults", {}).get("standin_url", "http://127.0.0.1:8000")

    standin_mgr = None
    if source == "standin":
        standin_mgr = StandinManager(base_url=standin_url)
        await standin_mgr.ensure_running()
        client = GroqClient(base_url=f"{standin_url}/v1/chat/completions")
    else:
        client = GroqClient()

    limiter = SharedLimiter()

    try:
        for s_name in scenario_names:
            if s_name not in scenarios_cfg.get("scenarios", {}):
                print(f"Warning: Scenario '{s_name}' not defined in scenarios.yaml. Skipping.")
                continue

            s_cfg = scenarios_cfg["scenarios"][s_name]
            await run_scenario(
                scenario_name=s_name,
                scenario_cfg=s_cfg,
                source=source,
                questions=questions,
                client=client,
                limiter=limiter,
                standin_mgr=standin_mgr,
                csv_path=csv_path,
                traces_path=traces_path,
                answers_path=answers_path,
                max_requests_cap=cap,
                scale_factor=scale_factor,
            )
            # Short cooldown between scenarios
            await asyncio.sleep(2.0)

    finally:
        if standin_mgr:
            await standin_mgr.shutdown()
        await client.close()

    print("\n================================================================================")
    print(f"LOAD RUN COMPLETE: Results saved to '{csv_path}'")
    print(f"Traces logged to '{traces_path}', Answers logged to '{answers_path}'")
    print("================================================================================\n")


def main():
    parser = argparse.ArgumentParser(description="Workload runner for the Q&A pipeline.")
    parser.add_argument(
        "--scenario",
        default="steady",
        help="Specific scenario to run ('steady', 'burst', 'sustained_overload', 'rpm_halved', 'daily_budget_exhausted', or 'all')",
    )
    parser.add_argument("--config", default="scenarios.yaml", help="Path to scenarios YAML configuration")
    parser.add_argument("--questions", default="questions.json", help="Path to questions JSON file")
    parser.add_argument("--csv", default="results.csv", help="Path to append CSV results")
    parser.add_argument("--traces", default="traces.jsonl", help="Path to append request traces")
    parser.add_argument("--answers", default="answers.jsonl", help="Path to append request answers")
    parser.add_argument("--real", action="store_true", help="Execute against REAL Groq API (requires --cap <= 30)")
    parser.add_argument("--cap", type=int, default=None, help="Explicit request cap (mandatory with --real, max 30)")
    parser.add_argument("--quick", action="store_true", help="Run quick preview with 0.25x durations/bursts")

    args = parser.parse_args()

    # Safety enforcement for real runs
    if args.real:
        if args.cap is None:
            print("ERROR: Real runs MUST specify an explicit request cap via --cap <N>.", file=sys.stderr)
            sys.exit(1)
        if args.cap > 30:
            print(f"ERROR: Real run cap cannot exceed 30 requests to protect quota (requested: {args.cap}).", file=sys.stderr)
            sys.exit(1)
        source = "real"
        print(f"\n[SAFETY NOTICE] Running against REAL Groq API with code-enforced cap of {args.cap} requests.")
    else:
        source = "standin"

    cfg = load_yaml_config(args.config)
    scale_factor = 0.25 if args.quick else 1.0

    if args.scenario.lower() == "all":
        scenario_list = ["steady", "burst", "sustained_overload", "rpm_halved", "daily_budget_exhausted"]
    else:
        scenario_list = [args.scenario]

    asyncio.run(
        run_load_suite(
            scenario_names=scenario_list,
            scenarios_cfg=cfg,
            source=source,
            questions_file=args.questions,
            csv_path=args.csv,
            traces_path=args.traces,
            answers_path=args.answers,
            cap=args.cap,
            scale_factor=scale_factor,
        )
    )


if __name__ == "__main__":
    main()
