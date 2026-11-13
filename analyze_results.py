import csv
from collections import defaultdict
import numpy as np

with open("results.csv", "r", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))

by_scen = defaultdict(list)
for r in rows:
    by_scen[r["scenario"]].append(r)

print("=" * 110)
print("TABLE 2A & 2B: BENCHMARK RESULTS FROM results.csv")
print("=" * 110)
print(f"{'Scenario':<24} | {'Total':<5} | {'OK':<4} | {'429 Shed':<8} | {'Timeout':<7} | {'Refused':<7} | {'TTFT p50 / p95 (ms)':<22} | {'Total p50 / p95 (ms)':<22}")
print("-" * 110)

for scen, s_rows in by_scen.items():
    total = len(s_rows)
    succeeded = sum(1 for r in s_rows if r["status"] == "ok")
    rate_limited = sum(1 for r in s_rows if r["status"] == "rate_limited")
    timed_out = sum(1 for r in s_rows if r["status"] == "timeout")
    refused = sum(1 for r in s_rows if r["status"].startswith("refused"))

    ttfts = [float(r["first_token_ms"]) for r in s_rows if r["first_token_ms"] and float(r["first_token_ms"]) > 0]
    totals = [float(r["total_ms"]) for r in s_rows if r["total_ms"] and float(r["total_ms"]) > 0]

    ttft_str = f"{np.percentile(ttfts, 50):.1f} / {np.percentile(ttfts, 95):.1f}" if ttfts else "N/A"
    tot_str = f"{np.percentile(totals, 50):.1f} / {np.percentile(totals, 95):.1f}" if totals else "N/A"

    print(f"{scen:<24} | {total:>5} | {succeeded:>4} | {rate_limited:>8} | {timed_out:>7} | {refused:>7} | {ttft_str:<22} | {tot_str:<22}")

print("=" * 110)

