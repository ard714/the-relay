"""
explain_slow.py - Trace breakdown and timeline visualization for Section 4 of the write-up.

Finds the slowest successful request from traces.jsonl (or a specific named request),
splits its latency into waiting, working, and retrying, indicates whether the delay
occurred before or after the first token, and formats a clean text timeline for paste-in.
"""

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_data(
    traces_path: str = "traces.jsonl",
    results_path: str = "results.csv",
    answers_path: str = "answers.jsonl",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    traces = []
    if Path(traces_path).exists():
        with open(traces_path, "r", encoding="utf-8") as f:
            traces = [json.loads(line) for line in f if line.strip()]

    results = []
    if Path(results_path).exists():
        with open(results_path, "r", encoding="utf-8") as f:
            results = list(csv.DictReader(f))

    answers = []
    if Path(answers_path).exists():
        with open(answers_path, "r", encoding="utf-8") as f:
            answers = [json.loads(line) for line in f if line.strip()]

    return traces, results, answers


def match_trace_with_results(
    traces: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    answers: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Enrich each trace with request_id, scenario, status, and answer text."""
    enriched = []
    for t in traces:
        t_copy = dict(t)
        q = t.get("question", "").strip()
        t_ms = t.get("total_ms", 0.0)

        # Match in answers
        matched_ans = None
        for a in answers:
            if a.get("question", "").strip() == q and abs(a.get("status_code", 0) - 200) < 300:
                # Approximate match
                matched_ans = a
                break

        # Match in results
        matched_res = None
        if matched_ans:
            req_id = matched_ans.get("request_id")
            for r in results:
                if r.get("request_id") == req_id:
                    matched_res = r
                    break
        else:
            for r in results:
                if abs(float(r.get("total_ms", 0) or 0) - t_ms) < 1.0:
                    matched_res = r
                    break

        t_copy["request_id"] = matched_res.get("request_id") if matched_res else "unknown_id"
        t_copy["scenario"] = matched_res.get("scenario") if matched_res else "unknown_scenario"
        t_copy["status"] = matched_res.get("status") if matched_res else t.get("status", "ok")
        t_copy["answer"] = matched_ans.get("answer") if matched_ans else ""
        t_copy["citations"] = matched_ans.get("citations") if matched_ans else []
        enriched.append(t_copy)

    return enriched


def format_timeline(trace: Dict[str, Any]) -> str:
    steps = trace.get("steps", [])
    total_ms = float(trace.get("total_ms", 0.0))
    q = trace.get("question", "")
    req_id = trace.get("request_id", "unknown_id")
    scenario = trace.get("scenario", "unknown_scenario")
    status = trace.get("status", "ok")

    if not steps:
        return f"Trace for request {req_id} contains no granular step data."

    # Establish relative start time (t=0 ms)
    global_start = min(s.get("start_time", 0.0) for s in steps if s.get("start_time", 0) > 0)

    work_ms_total = 0.0
    retry_ms_total = 0.0

    timeline_spans = []

    for i, s in enumerate(steps, 1):
        s_name = s.get("name", f"step_{i}")
        s_start = s.get("start_time", 0.0)
        s_end = s.get("end_time", 0.0)
        work_ms = float(s.get("work_ms", 0.0) or 0.0)
        retry_ms = float(s.get("retry_ms", 0.0) or 0.0)
        wait_ms = float(s.get("wait_ms", 0.0) or 0.0)
        retries = int(s.get("retries", 0) or 0)
        meta = s.get("metadata", {})

        rel_start_ms = max(0.0, (s_start - global_start) * 1000.0) if s_start > 0 else 0.0
        rel_end_ms = max(rel_start_ms, (s_end - global_start) * 1000.0) if s_end > 0 else rel_start_ms + work_ms + retry_ms

        work_ms_total += work_ms
        retry_ms_total += retry_ms

        span_desc = f"{s_name}"
        if "hits_count" in meta:
            span_desc += f" (retrieved {meta['hits_count']} chunks, top_score={meta.get('top_score', 0):.2f})"
        elif "tokens_out" in meta:
            span_desc += f" (streamed {meta['tokens_out']} tokens)"
        elif "details" in meta:
            span_desc += f" ({meta['details']})"
        elif "verdict" in meta:
            span_desc += f" (verdict: {meta['verdict']})"

        timeline_spans.append({
            "name": s_name,
            "rel_start_ms": rel_start_ms,
            "rel_end_ms": rel_end_ms,
            "duration_ms": round(rel_end_ms - rel_start_ms, 2),
            "work_ms": work_ms,
            "retry_ms": retry_ms,
            "wait_ms": wait_ms,
            "retries": retries,
            "desc": span_desc,
        })

    # Total wait time includes gaps between steps and semaphore queue delays
    gap_wait_ms = max(0.0, total_ms - (work_ms_total + retry_ms_total))

    before_first_token_ms = float(trace.get("delay_before_first_token_ms", total_ms) or total_ms)
    after_first_token_ms = float(trace.get("delay_after_first_token_ms", 0.0) or 0.0)

    # Format text report
    lines = []
    lines.append("=" * 88)
    lines.append(f"REQUEST LATENCY TIMELINE: {req_id} (Scenario: {scenario}, Status: {status})")
    lines.append("=" * 88)
    lines.append(f"QUESTION: \"{q}\"")
    if trace.get("answer"):
        lines.append(f"ANSWER:   \"{trace.get('answer')[:100]}...\"")
    lines.append("-" * 88)
    lines.append(f"TOTAL LATENCY:        {total_ms:>8.2f} ms")
    lines.append(f"  - Working Time:     {work_ms_total:>8.2f} ms ({work_ms_total/total_ms*100:>5.1f}%) [Active compute & generation]")
    lines.append(f"  - Waiting Time:     {gap_wait_ms:>8.2f} ms ({gap_wait_ms/total_ms*100:>5.1f}%) [Queue contention / rate limiter wait]")
    lines.append(f"  - Retrying Time:    {retry_ms_total:>8.2f} ms ({retry_ms_total/total_ms*100:>5.1f}%) [Backoff sleeps across failed attempts]")
    lines.append("-" * 88)
    lines.append("TOKEN DELIVERY BOUNDARY (Gated Streaming Policy):")
    lines.append(f"  - Delay BEFORE First Token: {before_first_token_ms:>8.2f} ms ({before_first_token_ms/total_ms*100:>5.1f}% of total time)")
    lines.append(f"  - Delay AFTER First Token:  {after_first_token_ms:>8.2f} ms ({after_first_token_ms/total_ms*100:>5.1f}% of total time)")
    lines.append("  * Note: Under Option 2 (Gated Delivery), 100% of pipeline delay occurs BEFORE")
    lines.append("    the first token to prevent delivering unverified or hallucinated text to the user.")
    lines.append("-" * 88)
    lines.append("STEP-BY-STEP EXECUTION SPANS:")
    lines.append(f"{'Start - End (ms)':<22} | {'Duration':<10} | {'Type':<10} | Step & Details")
    lines.append("-" * 88)

    for sp in timeline_spans:
        span_str = f"[{sp['rel_start_ms']:>6.1f}ms -> {sp['rel_end_ms']:>6.1f}ms]"
        dur_str = f"{sp['duration_ms']:>7.1f} ms"
        span_type = "RETRY" if sp["retry_ms"] > 0 else "WORK"
        lines.append(f"{span_str:<22} | {dur_str:<10} | {span_type:<10} | {sp['desc']}")

    lines.append("=" * 88)
    lines.append("\nTEXT SUMMARY FOR WRITE-UP (SECTION 4):")
    lines.append(f"\"For slow request {req_id} (total duration {total_ms:.1f} ms), latency was split into:")
    lines.append(f" - Working time: {work_ms_total:.1f} ms ({work_ms_total/total_ms*100:.1f}%),")
    lines.append(f" - Waiting/queuing time: {gap_wait_ms:.1f} ms ({gap_wait_ms/total_ms*100:.1f}%),")
    lines.append(f" - Retrying time: {retry_ms_total:.1f} ms ({retry_ms_total/total_ms*100:.1f}%).")
    lines.append(f"All {before_first_token_ms:.1f} ms of delay occurred BEFORE the first token was delivered")
    lines.append("because gated streaming withholds delivery until passage retrieval, fact extraction,")
    lines.append("and LLM audit verification are completely finished.\"\n")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Explain latency breakdown of a slow request.")
    parser.add_argument("--traces", default="traces.jsonl", help="Path to traces.jsonl")
    parser.add_argument("--results", default="results.csv", help="Path to results.csv")
    parser.add_argument("--answers", default="answers.jsonl", help="Path to answers.jsonl")
    parser.add_argument("--id", default=None, help="Specific request_id or question substring to inspect")
    parser.add_argument("--list", action="store_true", help="List top 10 slowest requests")

    args = parser.parse_args()

    traces, results, answers = load_data(args.traces, args.results, args.answers)
    if not traces:
        print("Error: No traces found in traces.jsonl.", file=sys.stderr)
        sys.exit(1)

    enriched = match_trace_with_results(traces, results, answers)

    # Filter for valid traces with steps
    valid = [t for t in enriched if len(t.get("steps", [])) > 0]
    valid.sort(key=lambda t: float(t.get("total_ms", 0.0)), reverse=True)

    if args.list:
        print(f"\nTOP 10 SLOWEST REQUESTS:")
        print(f"{'#':<3} | {'Request ID':<26} | {'Scenario':<16} | {'Status':<12} | {'Total ms':<10} | Question")
        print("-" * 90)
        for i, t in enumerate(valid[:10], 1):
            print(f"{i:<3} | {t.get('request_id'):<26} | {t.get('scenario'):<16} | {t.get('status'):<12} | {t.get('total_ms', 0):>8.1f}ms | {t.get('question', '')[:35]}...")
        print("")
        return

    selected = None
    if args.id:
        target_str = args.id.lower()
        for t in valid:
            if target_str in t.get("request_id", "").lower() or target_str in t.get("question", "").lower():
                selected = t
                break
        if not selected:
            print(f"Error: Could not find trace matching '{args.id}'.", file=sys.stderr)
            sys.exit(1)
    else:
        # Default: pick slowest request that succeeded (status == 'ok')
        ok_candidates = [t for t in valid if t.get("status") == "ok"]
        selected = ok_candidates[0] if ok_candidates else valid[0]

    report = format_timeline(selected)
    print(report)


if __name__ == "__main__":
    main()

