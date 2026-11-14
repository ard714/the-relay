# The Relay — Deliverables Submission Package

This folder contains the complete, self-contained submission deliverables for **The Relay** take-home assignment for Vaani.

---

## Folder Structure & Contents

```text
deliverables/
├── report/
│   ├── writeup.pdf                # Primary 2-page executive report (compiled from LaTeX)
│   ├── writeup.md                 # Markdown source adhering strictly to the 12 required headings
│   └── writeup.tex                # LaTeX source code for the 2-page report
│
├── data/
│   ├── results.csv                # Benchmark results (207 rows, exactly 12 required columns)
│   ├── answers.jsonl              # 207 complete generated answers, citations, and status codes
│   ├── traces.jsonl               # 207 step-by-step latency execution traces
│   ├── raw_real.jsonl             # 68 physical Groq probe requests with rate-limit headers
│   ├── raw_standin.jsonl          # Raw execution logs from local stand-in service
│   ├── groq_baseline.json         # Real Groq limits, latencies, and concurrency ramp profile
│   ├── validation_report.json     # Side-by-side comparison of real vs stand-in distributions
│   ├── ques.yaml                  # 15 ground-truth verified questions (Attention, Moshi, DynChunk)
│   ├── questions.json             # 18-question evaluation pool for Table 3
│   └── grades.json                # First-pass automated answer grades and evaluation report
│
├── prompts/
│   ├── conversation_history.md    # Human-readable prompt & response transcript (24 user turns)
│   └── conversation_transcript.jsonl # Full raw system-generated JSONL transcript (1,090 events)
│
└── code/
    ├── config.py                  # Retrieval settings (A, B, C), limits, pricing, and models
    ├── pipeline.py                # 5-stage agentic pipeline with 2-stage verification loop
    ├── limiter.py                 # Shared in-flight cap, sliding RPM/TPM, deadline shedding
    ├── retrieve.py                # Lexical BM25 and TF-IDF Cosine re-ranking with exact citations
    ├── parse_papers.py            # PyMuPDF extractor tracking active section headers & pages
    ├── standin.py                 # Controllable aiohttp mock server with SSE streaming & admin API
    ├── groq_client.py             # Async streaming httpx client with SSE instrumentation
    ├── load_runner.py             # Workload runner for steady, burst, overload, halved, exhausted
    ├── scenarios.yaml             # Workload definitions and arrival parameters
    ├── eval_retrieval.py          # Table 3 evaluation harness across Settings A, B, and C
    ├── compute_tables.py          # Nearest-rank table generator with strict --check mode
    ├── explain_slow.py            # Monotonic trace timeline decomposer (working/waiting/retrying)
    ├── check_questions.py         # PyMuPDF PDF ground-truth verifier for must_contain strings
    ├── grade_answers.py           # First-pass side-by-side answer grader with overrule support
    ├── calibrate.py               # Empirical lognormal TTFT and per-token speed fitter
    ├── validate_standin.py        # Side-by-side 30-request validation of real vs stand-in
    ├── test_standin.py            # Unit tests for standin server endpoints
    ├── test_limiter_policy.py     # Unit tests: deadline shedding, step isolation, shared cooldown
    ├── DECISIONS.md               # Architectural decisions, trade-offs, rationale, and costs
    └── PROBLEMS_AND_FIXES.txt     # Audit log of bugs caught and fixes applied
```

---

## Quick Verification Commands

From the repository root, all checks can be reproduced cleanly:

1. **Verify Table Results from `results.csv`:**
   ```bash
   python compute_tables.py --check
   ```
   *Validates all 12 columns and outputs Table 2a, Table 2b, and Table 3 using Nearest-Rank percentiles.*

2. **Run Unit & Resilience Tests:**
   ```bash
   python -m unittest test_standin.py test_limiter_policy.py
   ```
   *Verifies deadline-aware 1.3ms shedding, step-isolated retries, shared cooldowns, and stand-in SSE streaming.*

3. **Verify Ground-Truth Questions Against PDFs:**
   ```bash
   python check_questions.py
   ```
   *Confirms 100% of answerable phrases appear in the cited paper PDFs and unanswerable terms are absent.*

4. **Decompose Slow Request Latency:**
   ```bash
   python explain_slow.py
   ```
   *Renders the exact timeline decomposition for `req_burst_009_ec524f`.*

---

## Key Highlights & Architectural Decisions

* **Option 2 (Gated Delivery):** Tokens are only streamed to the user AFTER full two-stage verification completes. Correctness is an absolute invariant.
* **Deadline-Aware Shedding:** On HTTP 429, if `retry_after > remaining_deadline`, requests are shed immediately in $\le 1.3\text{ ms}$ at zero token cost rather than queuing for users who have already abandoned.
* **Empirically Calibrated Stand-in:** Lognormal TTFT ($\mu = 6.4399, \sigma = 0.3396$), $0.9247\text{ ms/token}$, $+25\text{ ms}$ concurrency slowdown, and exact Groq 429 payload.
* **Zero Secret Leakage:** `GROQ_API_KEY` is loaded exclusively from the environment. Zero keys exist in files, datasets, or commit history.
