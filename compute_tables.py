"""
compute_tables.py - Compute Tables 2a, 2b, and 3 strictly from results.csv.

Methodology:
- Percentiles: Nearest-Rank (rank = ceil(P / 100 * N)).
- Peak queue length: Max concurrent requests in flight, computed from start_time and total_ms.
- Offered rate: requests / (last start - first start), expressed in requests per minute.
- Formatting: Milliseconds to 1 decimal, percentages to 1 decimal, 'not measured' for empty cells.
- Pricing: Input $0.05 / 1M tokens, Output $0.08 / 1M tokens (from config.py).
- Check mode (--check): Fails loudly if any required column is missing or any status is invalid.
"""

import argparse
import csv
from datetime import datetime
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import INPUT_TOKEN_PRICE_PER_M, OUTPUT_TOKEN_PRICE_PER_M, RETRIEVAL_SETTINGS

REQUIRED_COLUMNS = [
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

ALLOWED_STATUSES = {"ok", "rate_limited", "timeout", "error"}


def parse_iso_timestamp(ts_str: str) -> float:
    """Parse ISO8601 UTC timestamp string to epoch seconds."""
    s = ts_str.strip()
    # Handle Z suffix
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s).timestamp()


def nearest_rank_percentile(values: List[float], p: float) -> Optional[float]:
    """
    Compute nearest-rank percentile.
    Method:
      1. Sort values in ascending order.
      2. Rank index: k = ceil((P / 100) * N)
      3. Return v[k - 1] (0-indexed).
    """
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    rank = math.ceil((p / 100.0) * n)
    idx = max(0, min(rank - 1, n - 1))
    return sorted_vals[idx]


def compute_peak_queue_length(rows: List[Dict[str, Any]]) -> int:
    """
    Compute maximum number of requests in the system concurrently
    from start_time and total_ms.
    """
    events = []
    for r in rows:
        try:
            t_start = parse_iso_timestamp(r["start_time"])
            tot_sec = float(r["total_ms"]) / 1000.0 if r.get("total_ms") else 0.0
            t_end = t_start + tot_sec
            events.append((t_start, 1))   # Arrival
            events.append((t_end, -1))   # Departure
        except Exception:
            continue

    if not events:
        return 0

    # Sort events by time; on tie, arrival (+1) comes first to capture true peak overlap
    events.sort(key=lambda x: (x[0], -x[1]))

    current = 0
    peak = 0
    for t, delta in events:
        current += delta
        if current > peak:
            peak = current

    return peak


def compute_offered_rate_rpm(rows: List[Dict[str, Any]]) -> Optional[float]:
    """
    Offered rate = requests / (last start - first start), in requests/minute.
    """
    if len(rows) < 2:
        return None
    starts = []
    for r in rows:
        try:
            starts.append(parse_iso_timestamp(r["start_time"]))
        except Exception:
            pass

    if len(starts) < 2:
        return None

    starts.sort()
    delta_sec = starts[-1] - starts[0]
    if delta_sec <= 0.001:
        # Instantaneous burst
        return None

    rate_per_sec = len(rows) / delta_sec
    return rate_per_sec * 60.0


def validate_results_csv(filepath: str) -> List[Dict[str, Any]]:
    """Strict validation for --check mode."""
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"CHECK FAILED: File '{filepath}' does not exist.")

    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []

        # 1. Check all required columns exist
        missing = [col for col in REQUIRED_COLUMNS if col not in header]
        if missing:
            raise ValueError(
                f"CHECK FAILED: results.csv is missing required columns: {missing}\n"
                f"Found header: {header}"
            )

        rows = list(reader)

    if not rows:
        raise ValueError("CHECK FAILED: results.csv is empty (contains no data rows).")

    # 2. Check each row's status
    for idx, r in enumerate(rows, 1):
        st = r.get("status")
        if st not in ALLOWED_STATUSES:
            raise ValueError(
                f"CHECK FAILED: Row {idx} (request_id={r.get('request_id')}) has invalid status '{st}'.\n"
                f"Must be one of strictly: {sorted(list(ALLOWED_STATUSES))}"
            )

    return rows


def generate_table_2a(rows: List[Dict[str, Any]]) -> str:
    """
    Table 2a: Latency, one row per load scenario
    Columns: Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms)
    """
    # Filter scenarios for Table 2
    scenarios_order = ["steady", "burst", "sustained_overload", "rpm_halved", "daily_budget_exhausted"]
    by_scenario = {sc: [] for sc in scenarios_order}
    for r in rows:
        sc = r["scenario"]
        if sc in by_scenario:
            by_scenario[sc].append(r)

    lines = []
    lines.append("### Table 2a: Latency, one row per load scenario")
    lines.append("")
    lines.append("| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")

    for sc in scenarios_order:
        s_rows = by_scenario[sc]
        if not s_rows:
            lines.append(f"| {sc} | not measured | not measured | not measured | not measured | not measured | not measured |")
            continue

        source = s_rows[0].get("source", "standin")
        n_req = len(s_rows)

        # Duration
        t_starts = [parse_iso_timestamp(r["start_time"]) for r in s_rows if r.get("start_time")]
        t_ends = [
            parse_iso_timestamp(r["start_time"]) + (float(r["total_ms"]) / 1000.0)
            for r in s_rows
            if r.get("start_time") and r.get("total_ms")
        ]
        duration_sec = (max(t_ends) - min(t_starts)) if (t_starts and t_ends) else 0.0

        # Offered rate
        offered_rpm = compute_offered_rate_rpm(s_rows)
        offered_str = f"{offered_rpm:.1f}" if offered_rpm is not None else "Burst (instant)"

        # Latencies (TTFT only measured on successful 'ok' responses that actually emitted tokens)
        ttfts = [
            float(r["first_token_ms"])
            for r in s_rows
            if r.get("status") == "ok" and r.get("first_token_ms") and float(r["first_token_ms"]) > 0
        ]
        totals = [float(r["total_ms"]) for r in s_rows if r.get("total_ms") and float(r["total_ms"]) > 0]

        if ttfts:
            ttft_p50 = nearest_rank_percentile(ttfts, 50)
            ttft_p95 = nearest_rank_percentile(ttfts, 95)
            ttft_p99 = nearest_rank_percentile(ttfts, 99)
            ttft_str = f"{ttft_p50:.1f} / {ttft_p95:.1f} / {ttft_p99:.1f}"
        else:
            ttft_str = "not measured"

        if totals:
            tot_p50 = nearest_rank_percentile(totals, 50)
            tot_p95 = nearest_rank_percentile(totals, 95)
            tot_p99 = nearest_rank_percentile(totals, 99)
            tot_str = f"{tot_p50:.1f} / {tot_p95:.1f} / {tot_p99:.1f}"
        else:
            tot_str = "not measured"

        lines.append(
            f"| **{sc}** | {source} | {n_req} | {duration_sec:.1f} | {offered_str} | {ttft_str} | {tot_str} |"
        )

    return "\n".join(lines)


def generate_table_2b(rows: List[Dict[str, Any]]) -> str:
    """
    Table 2b: Outcomes, same rows
    Columns: Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers
    """
    scenarios_order = ["steady", "burst", "sustained_overload", "rpm_halved", "daily_budget_exhausted"]
    by_scenario = {sc: [] for sc in scenarios_order}
    for r in rows:
        sc = r["scenario"]
        if sc in by_scenario:
            by_scenario[sc].append(r)

    lines = []
    lines.append("### Table 2b: Outcomes, same rows")
    lines.append("")
    lines.append("| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    for sc in scenarios_order:
        s_rows = by_scenario[sc]
        if not s_rows:
            lines.append(f"| {sc} | not measured | not measured | not measured | not measured | not measured | not measured | not measured |")
            continue

        n_req = len(s_rows)
        succeeded_count = sum(1 for r in s_rows if r["status"] == "ok")
        rate_limited_count = sum(1 for r in s_rows if r["status"] == "rate_limited")
        timeout_count = sum(1 for r in s_rows if r["status"] == "timeout")

        succ_pct = (succeeded_count / n_req) * 100.0
        rl_pct = (rate_limited_count / n_req) * 100.0
        to_pct = (timeout_count / n_req) * 100.0

        # Retries
        total_retries = sum(int(r.get("retries", 0) or 0) for r in s_rows)
        retries_per_req = total_retries / n_req

        # Peak queue length
        peak_q = compute_peak_queue_length(s_rows)

        # Tokens per answer (for succeeded/completed answers)
        tokens_out_list = [int(r.get("tokens_out", 0) or 0) for r in s_rows if r["status"] == "ok"]
        avg_tokens_out = (sum(tokens_out_list) / len(tokens_out_list)) if tokens_out_list else 0.0

        # Cost per 1,000 answers
        # Formula: (tokens_in * input_price + tokens_out * output_price) / 1M tokens
        costs = []
        for r in s_rows:
            t_in = int(r.get("tokens_in", 0) or 0)
            t_out = int(r.get("tokens_out", 0) or 0)
            req_cost = (t_in / 1_000_000.0) * INPUT_TOKEN_PRICE_PER_M + (t_out / 1_000_000.0) * OUTPUT_TOKEN_PRICE_PER_M
            costs.append(req_cost)

        avg_cost_per_req = (sum(costs) / len(costs)) if costs else 0.0
        cost_per_1000 = avg_cost_per_req * 1000.0

        tok_str = f"{avg_tokens_out:.1f}" if avg_tokens_out > 0 else "0.0"
        cost_str = f"${cost_per_1000:.4f}"

        lines.append(
            f"| **{sc}** | {succ_pct:.1f}% | {rl_pct:.1f}% | {to_pct:.1f}% | {retries_per_req:.2f} | {peak_q} | {tok_str} | {cost_str} |"
        )

    return "\n".join(lines)


def generate_table_3(rows: List[Dict[str, Any]]) -> str:
    """
    Table 3: Retrieval settings (at least three)
    Columns: Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms)
    """
    settings_order = ["A", "B", "C"]
    by_setting = {st: [] for st in settings_order}

    # First look for dedicated evaluation rows (scenario starts with eval_setting_) or by retrieval_setting
    for r in rows:
        st = r.get("retrieval_setting")
        sc = r.get("scenario", "")
        if st in by_setting:
            if sc.startswith("eval_setting_") or len([x for x in rows if x.get("scenario", "").startswith("eval_setting_")]) == 0:
                by_setting[st].append(r)

    lines = []
    lines.append("### Table 3: Retrieval settings (at least three)")
    lines.append("")
    lines.append("| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |")
    lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

    setting_descriptions = {
        "A": "Setting A (120 tok, 3 pcs, no rerank)",
        "B": "Setting B (280 tok, 5 pcs, no rerank)",
        "C": "Setting C (350 tok, 7 pcs, +rerank)",
    }

    for st in settings_order:
        s_rows = by_setting[st]
        desc = setting_descriptions.get(st, f"Setting {st}")
        if not s_rows:
            lines.append(f"| {desc} | not measured | not measured | not measured | not measured | not measured | not measured | not measured |")
            continue

        n_q = len(s_rows)
        # Outcome breakdown
        correct_cited = sum(1 for r in s_rows if r.get("answer_grade") in ("correct", "degraded_correct"))
        wrong = sum(1 for r in s_rows if r.get("answer_grade") in ("wrong", "refused_wrongly"))
        refused_corr = sum(1 for r in s_rows if r.get("answer_grade") in ("refused_correctly", "budget_exhausted"))

        corr_pct = (correct_cited / n_q) * 100.0
        wrong_pct = (wrong / n_q) * 100.0
        ref_pct = (refused_corr / n_q) * 100.0

        # Tokens per answer
        tok_list = [int(r.get("tokens_out", 0) or 0) for r in s_rows if r.get("status") == "ok"]
        avg_tokens = (sum(tok_list) / len(tok_list)) if tok_list else 0.0

        # Latency percentiles (TTFT only measured on successful 'ok' responses that emitted tokens)
        ttfts = [
            float(r["first_token_ms"])
            for r in s_rows
            if r.get("status") == "ok" and r.get("first_token_ms") and float(r["first_token_ms"]) > 0
        ]
        totals = [float(r["total_ms"]) for r in s_rows if r.get("total_ms") and float(r["total_ms"]) > 0]

        if ttfts:
            ttft_p50 = nearest_rank_percentile(ttfts, 50)
            ttft_p95 = nearest_rank_percentile(ttfts, 95)
            ttft_str = f"{ttft_p50:.1f} / {ttft_p95:.1f}"
        else:
            ttft_str = "not measured"

        if totals:
            tot_p50 = nearest_rank_percentile(totals, 50)
            tot_p95 = nearest_rank_percentile(totals, 95)
            tot_str = f"{tot_p50:.1f} / {tot_p95:.1f}"
        else:
            tot_str = "not measured"

        lines.append(
            f"| **{desc}** | {n_q} | {corr_pct:.1f}% | {wrong_pct:.1f}% | {ref_pct:.1f}% | {avg_tokens:.1f} | {ttft_str} | {tot_str} |"
        )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Compute Tables 2a, 2b, and 3 from results.csv.")
    parser.add_argument("--csv", default="results.csv", help="Path to results.csv")
    parser.add_argument("--check", action="store_true", help="Fail loudly if any column missing or status invalid")
    args = parser.parse_args()

    if args.check:
        try:
            rows = validate_results_csv(args.csv)
            print(f"[CHECK PASSED] Validated {len(rows)} rows from '{args.csv}'. All required columns present and status values strictly conform to ok/rate_limited/timeout/error.\n")
        except Exception as e:
            print(f"\n[FATAL ERROR] {e}\n", file=sys.stderr)
            sys.exit(1)
    else:
        path = Path(args.csv)
        if not path.exists():
            print(f"Error: File '{args.csv}' not found.", file=sys.stderr)
            sys.exit(1)
        with open(path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    print("================================================================================")
    print("RESULTS TABLES FOR SUBMISSION (Computed strictly from results.csv)")
    print("Percentile calculation method: Nearest-Rank (rank = ceil(P / 100 * N))")
    print(f"Pricing config: Input=${INPUT_TOKEN_PRICE_PER_M}/1M tokens, Output=${OUTPUT_TOKEN_PRICE_PER_M}/1M tokens")
    print("================================================================================\n")

    t2a = generate_table_2a(rows)
    t2b = generate_table_2b(rows)
    t3 = generate_table_3(rows)

    print(t2a)
    print("\n" + "-" * 80 + "\n")
    print(t2b)
    print("\n" + "-" * 80 + "\n")
    print(t3)
    print("\n================================================================================\n")


if __name__ == "__main__":
    main()
