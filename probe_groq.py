"""
probe_groq.py - Probe Groq API to measure baseline latency, concurrency limits, and 429 behavior.

Features:
- Uses groq_client.GroqClient (zero retries, logs to data/raw_real.jsonl)
- Enforces a HARD CAP of 80 requests total
- Halts immediately on any daily-limit exhaustion error
- Phase 1: 25 sequential short requests for latency distribution
- Phase 2: Concurrency ramp (2, 4, 8, 16) until errors hit
- Phase 3: Captures per-minute 429, logs error body & retry-after, sleeps exact duration, sends 1 recovery request
- Prints summary of raw headers, 429 payload, recovery time
- Saves structured results to groq_baseline.json
"""

import asyncio
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import GROQ_MODEL
from groq_client import GroqClient

HARD_REQUEST_CAP = 80
_REQUESTS_SENT = 0

PROMPTS = [
    "Explain multi-head attention in one sentence.",
    "What is the key idea behind the Moshi model in one sentence?",
    "Why use dynamic chunking in sequence modeling? One sentence.",
    "State the computational complexity of dot-product attention.",
    "What is the role of residual connections in Transformers?",
    "Explain what a split RVQ does in speech tokenization.",
    "What is the advantage of byte-level modeling over tokenizers?",
    "Explain the role of positional encodings in Transformers.",
    "What is inner monologue in real-time dialogue models?",
    "How does scaled dot-product prevent small gradients with large dimensions?",
]


def check_and_increment_cap(count: int = 1) -> None:
    global _REQUESTS_SENT
    if _REQUESTS_SENT + count > HARD_REQUEST_CAP:
        raise RuntimeError(
            f"Hard request cap ({HARD_REQUEST_CAP}) reached! Current count: {_REQUESTS_SENT}. Aborting."
        )
    _REQUESTS_SENT += count


def is_daily_limit_error(result: Dict[str, Any]) -> bool:
    """Check if a response indicates daily quota exhaustion (not RPM/TPM rate limits)."""
    if result.get("status_code") != 429:
        return False
    body = (result.get("error_body") or "").lower()
    if (
        "requests per day" in body
        or "tokens per day" in body
        or "rpd" in body
        or "tpd" in body
        or "daily quota" in body
    ):
        return True
    return False


def parse_time_duration_str(duration_str: Optional[str], error_body: Optional[str] = None) -> float:
    """Parse duration from retry-after header or error message body (e.g. 'Please try again in 2s.')."""
    if duration_str:
        s = duration_str.strip()
        try:
            return float(s)
        except ValueError:
            pass

        total = 0.0
        m_match = re.search(r"(\d+(?:\.\d+)?)m", s)
        if m_match:
            total += float(m_match.group(1)) * 60.0
        s_match = re.search(r"(\d+(?:\.\d+)?)s", s)
        if s_match and not re.search(r"\d+ms", s):
            total += float(s_match.group(1))
        ms_match = re.search(r"(\d+(?:\.\d+)?)ms", s)
        if ms_match:
            total += float(ms_match.group(1)) / 1000.0

        if total > 0:
            return total

    if error_body:
        match = re.search(r"try again in (\d+(?:\.\d+)?)(m?s)", error_body)
        if match:
            val = float(match.group(1))
            unit = match.group(2)
            return val / 1000.0 if unit == "ms" else val

    return 2.0


async def run_sequential_phase(
    client: GroqClient, count: int = 25
) -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    """Phase 1: Run sequential requests to establish baseline latency and read headers."""
    print(f"\n--- Phase 1: {count} Sequential Latency Probes ---")
    results = []
    latest_headers: Dict[str, str] = {}

    for i in range(count):
        check_and_increment_cap(1)
        prompt = PROMPTS[i % len(PROMPTS)]
        print(f"[{i+1}/{count}] Sending request...", end="", flush=True)

        res = await client.stream_chat(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
        )
        results.append(res)

        if res["rate_limit_headers"]:
            latest_headers = res["rate_limit_headers"]

        if is_daily_limit_error(res):
            print(f"\nCRITICAL: Daily limit hit: {res.get('error_body')}", file=sys.stderr)
            sys.exit(1)

        if res.get("status_code") == 429:
            backoff = parse_time_duration_str(res.get("retry_after"), res.get("error_body"))
            print(f" Status: Rate-limited (429) | Backing off {backoff:.2f}s...")
            await asyncio.sleep(backoff + 0.5)
        else:
            print(
                f" Status: {res['status']} ({res['status_code']}) | TTFT: {res['ttft_ms']} ms | Total: {res['total_ms']} ms"
            )
            await asyncio.sleep(0.3)

    return results, latest_headers


async def run_concurrency_phase(
    client: GroqClient, levels: List[int] = [2, 4, 8, 16]
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Phase 2: Ramp concurrency (2, 4, 8, 16) until errors hit."""
    print("\n--- Phase 2: Concurrency Ramp (2, 4, 8, 16) ---")
    concurrency_reports = []
    first_429_result: Optional[Dict[str, Any]] = None

    for level in levels:
        global _REQUESTS_SENT
        if _REQUESTS_SENT + level > HARD_REQUEST_CAP:
            print(f"Skipping level {level} to stay within hard cap of {HARD_REQUEST_CAP}.")
            break

        check_and_increment_cap(level)
        print(f"\nDispatching batch of {level} concurrent requests...")

        tasks = [
            client.stream_chat(
                messages=[{"role": "user", "content": PROMPTS[k % len(PROMPTS)]}],
                max_tokens=80,
            )
            for k in range(level)
        ]

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        succeeded = 0
        failed = 0
        status_codes = []
        errors = []

        for r in batch_results:
            if isinstance(r, Exception):
                failed += 1
                status_codes.append("exception")
                errors.append(str(r))
            else:
                code = r.get("status_code")
                status_codes.append(code)
                if r.get("status") == "ok":
                    succeeded += 1
                else:
                    failed += 1
                    errors.append(r.get("error_body"))
                    if code == 429 and first_429_result is None:
                        first_429_result = r

                if is_daily_limit_error(r):
                    print(f"\nCRITICAL: Daily limit hit: {r.get('error_body')}", file=sys.stderr)
                    sys.exit(1)

        rep = {
            "concurrency": level,
            "requests_sent": level,
            "succeeded": succeeded,
            "failed": failed,
            "status_codes": status_codes,
            "errors": errors[:2],
        }
        concurrency_reports.append(rep)
        print(
            f"  Concurrency {level}: Succeeded: {succeeded}/{level} | Failed: {failed}/{level} | Statuses: {status_codes}"
        )

        if failed > 0:
            print(f"  Error detected at concurrency {level}. Halting ramp.")
            break

        await asyncio.sleep(2.0)

    return concurrency_reports, first_429_result


async def handle_rate_limit_and_recovery(
    client: GroqClient, observed_429: Optional[Dict[str, Any]] = None
) -> Optional[Dict[str, Any]]:
    """Phase 3: Verify 429 behavior, log payload, sleep exact retry-after, send 1 recovery request."""
    print("\n--- Phase 3: 429 Rate Limit Payload & Exact Recovery Test ---")
    global _REQUESTS_SENT

    hit_429_result = observed_429

    # If concurrency ramp didn't trigger a 429, fire a burst
    if not hit_429_result:
        remaining_budget = HARD_REQUEST_CAP - _REQUESTS_SENT - 1
        burst_size = min(12, max(2, remaining_budget))
        check_and_increment_cap(burst_size)
        print(f"Firing deliberate burst of {burst_size} requests to trigger 429...")

        heavy_prompt = (
            "Explain scaled dot-product attention mathematically with queries, keys, values, and dimension d_k."
        )
        tasks = [
            client.stream_chat(
                messages=[{"role": "user", "content": f"{heavy_prompt} [{i}]"}],
                max_tokens=200,
            )
            for i in range(burst_size)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, dict) and r.get("status_code") == 429:
                hit_429_result = r
                break

    if not hit_429_result:
        print("Notice: No 429 triggered within current window.")
        return None

    # We have a real 429!
    print("\n" + "!" * 65)
    print("CAPTURED REAL HTTP 429 RATE LIMIT RESPONSE")
    print("!" * 65)
    print(f"Raw 429 Body:\n{hit_429_result.get('error_body')}")

    rate_headers = hit_429_result.get("rate_limit_headers", {})
    retry_after_hdr = hit_429_result.get("retry_after") or rate_headers.get("retry-after")
    reset_tokens_hdr = rate_headers.get("x-ratelimit-reset-tokens")
    reset_reqs_hdr = rate_headers.get("x-ratelimit-reset-requests")

    print(f"\nRetry-After Header: {retry_after_hdr}")
    print(f"x-ratelimit-reset-tokens: {reset_tokens_hdr}")
    print(f"x-ratelimit-reset-requests: {reset_reqs_hdr}")

    raw_body = hit_429_result.get("error_body")
    sleep_duration = parse_time_duration_str(retry_after_hdr, raw_body)
    print(f"\nSleeping exact duration: {sleep_duration:.3f} seconds for recovery...")
    await asyncio.sleep(sleep_duration)

    # Dispatch 1 recovery request
    check_and_increment_cap(1)
    print("Sending 1 recovery request...")
    rec_start = time.perf_counter()
    recovery_res = await client.stream_chat(
        messages=[{"role": "user", "content": "Ping: Are you recovered?"}],
        max_tokens=30,
    )
    rec_end = time.perf_counter()
    rec_latency_ms = round((rec_end - rec_start) * 1000, 2)

    print(
        f"Recovery Request Status: {recovery_res['status']} ({recovery_res['status_code']}) | Latency: {rec_latency_ms} ms"
    )

    return {
        "status_code": 429,
        "raw_error_body": hit_429_result.get("error_body"),
        "retry_after_header": retry_after_hdr,
        "reset_tokens_header": reset_tokens_hdr,
        "reset_requests_header": reset_reqs_hdr,
        "sleep_duration_seconds": round(sleep_duration, 3),
        "recovery_status": recovery_res["status"],
        "recovery_status_code": recovery_res["status_code"],
        "recovery_latency_ms": rec_latency_ms,
    }


async def main_probe():
    client = GroqClient(model=GROQ_MODEL)
    print("=" * 70)
    print(f"GROQ BASELINE CALIBRATION SUITE — Model: {GROQ_MODEL}")
    print(f"Hard code-enforced cap: {HARD_REQUEST_CAP} requests")
    print("=" * 70)

    try:
        # Phase 1: Sequential
        seq_results, latest_headers = await run_sequential_phase(client, count=25)

        # Phase 2: Concurrency Ramp
        conc_results, ramp_429 = await run_concurrency_phase(client, levels=[2, 4, 8, 16])

        # Phase 3: Rate limit capture and recovery
        burst_report = await handle_rate_limit_and_recovery(client, observed_429=ramp_429)

        # Compute summary metrics
        valid_ttft = [r["ttft_ms"] for r in seq_results if r.get("ttft_ms") is not None]
        valid_total = [r["total_ms"] for r in seq_results if r.get("status") == "ok"]

        p50_ttft = round(statistics.median(valid_ttft), 2) if valid_ttft else None
        p95_ttft = round(statistics.quantiles(valid_ttft, n=20)[18], 2) if len(valid_ttft) >= 20 else None
        p50_total = round(statistics.median(valid_total), 2) if valid_total else None
        p95_total = round(statistics.quantiles(valid_total, n=20)[18], 2) if len(valid_total) >= 20 else None

        print("\n" + "=" * 70)
        print("SUMMARY: MEASURED GROQ BASELINE METRICS (Table 1 Data)")
        print("=" * 70)
        print("Raw Rate Limit Headers (Directly from Groq):")
        for k in sorted(latest_headers.keys()):
            print(f"  {k}: {latest_headers[k]}")

        print("\nLatency Metrics (Phase 1 Sequential, ms):")
        print(f"  TTFT p50: {p50_ttft} ms | p95: {p95_ttft} ms")
        print(f"  Total Time p50: {p50_total} ms | p95: {p95_total} ms")

        if burst_report:
            print("\n429 Rate-Limit & Recovery Metrics:")
            print(f"  HTTP 429 Payload: {burst_report['raw_error_body']}")
            print(f"  Retry-After: {burst_report['retry_after_header']}")
            print(f"  Sleep Duration: {burst_report['sleep_duration_seconds']} s")
            print(f"  Recovery Outcome: Status {burst_report['recovery_status_code']} ({burst_report['recovery_status']})")

        # Save to groq_baseline.json
        baseline_data = {
            "model": GROQ_MODEL,
            "probe_timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "total_requests_sent": _REQUESTS_SENT,
            "raw_rate_limit_headers": latest_headers,
            "latency_p50_p95_ms": {
                "ttft_p50": p50_ttft,
                "ttft_p95": p95_ttft,
                "total_p50": p50_total,
                "total_p95": p95_total,
            },
            "concurrency_ramp": conc_results,
            "rate_limit_burst_and_recovery": burst_report,
        }

        with open("groq_baseline.json", "w", encoding="utf-8") as f:
            json.dump(baseline_data, f, indent=2)
        print("\nSaved baseline results to groq_baseline.json")

    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main_probe())
