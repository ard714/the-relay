# Architectural & Engineering Decisions Log

This document tracks all key technical choices, trade-offs, rationale, and associated costs as mandated by the project constraints.

---

### 2026-11-14: Model Selection for Baseline Calibration
* **Decision:** Selected `openai/gpt-oss-20b` on Groq (confirmed by user) after verifying that `llama-3.1-8b-instant` returned 404 (unavailable on Groq endpoint).
* **Why:** High throughput, 1,000 RPM / 8,000 TPM free tier limits, fast TTFT (~70-100ms), and full compatibility with OpenAI-compatible chat completion payload.
* **What it costs:** 20B parameters; balances speed against the reasoning depth of a 120B/70B model.

---

### 2026-11-14: Stand-in Architecture
* **Decision:** Implement the stand-in model service as a standalone local HTTP server (`aiohttp`) with an async simulator core.
* **Why:** Enforces true protocol and transport parity with Groq over `httpx`, subjecting the client pipeline to real socket connection pooling, HTTP headers, chunked SSE streaming, and network timeouts.
* **What it costs:** Requires managing a local background server process and local port binding during testing rather than an in-memory mock.

---

### 2026-11-14: Document Ingestion and Heading Tracking
* **Decision:** Parse the 3 arXiv papers with PyMuPDF (`fitz`), tracking the active section heading in force at the top of each page using a hybrid heuristic (font size >= 1.12x body font, bold flags, and regex for standard/numbered headings).
* **Why:** Enables precise paper/page/section attribution for every retrieved passage without brittle OCR or opaque external parsers.
* **What it costs:** Requires downloading the PyMuPDF native wheel and managing PDF layout edge cases (e.g. multi-column text, formulas).

---

### 2026-11-14: Passage Retrieval & Local Re-ranking
* **Decision:** Implemented lexical BM25 (`rank-bm25`) paired with local TF-IDF Cosine re-ranking (`scikit-learn`) across 3 configurable settings (A, B, C) with strict citation tracking (`[Paper, §Section, p. Page]`).
* **Why:** Zero embedding API overhead, completely local, deterministic, and preserves exact provenance for all retrieved passages.
* **What it costs:** Pure lexical overlap cannot bridge vocabulary gaps where queries use synonyms absent from paper text.

---

### 2026-11-14: Empirically Measured Real Groq Service Limits
* **Decision:** Measured and logged real Groq limits and latency distributions for model `openai/gpt-oss-20b` without extrapolation.
* **Findings:**
  - **Requests Per Minute (RPM):** 30 (explicitly reported in Groq's 429 error payload).
  - **Tokens Per Minute (TPM):** 8,000 (`x-ratelimit-limit-tokens`).
  - **Requests Per Window:** 1,000 (`x-ratelimit-limit-requests` with ~2.5h rolling reset).
  - **Latency (25 Sequential Requests):** TTFT p50 = 587.64 ms, p95 = 1545.20 ms; Total time p50 = 741.36 ms, p95 = 1606.30 ms.
  - **Concurrency Ceiling:** Concurrency 2 (2/2 ok), Concurrency 4 (4/4 ok), Concurrency 8 (2/8 ok, 6/8 rate-limited 429).
  - **HTTP 429 Behavior & Recovery:** Returns code `rate_limit_exceeded`, `retry-after: 2` seconds. Sleeping the exact 2.0s duration yielded 100% recovery with 475.24 ms latency.

---

### 2026-11-14: Verification Output Gating (Streaming AFTER Verification)
* **Decision:** Stream tokens to the user only AFTER complete verification (Option 2) rather than speculative early streaming.
* **Why:** The brief explicitly states the assistant *"never wants to send a wrong or embarrassing answer just to stay fast."* Delivering tokens before verification could expose hallucinations or false claims to the user before they are caught by the verifier.
* **What it costs:** Time to First Token (TTFT) perceived by the user now includes the full verification pipeline (query understanding + retrieval + draft generation + verification checks), shifting latency from after first token to before first token.

---

### 2026-11-14: Shared Rate Limiting, Deadline-Aware Shedding & Graceful Degradation
* **Decision:** Built `limiter.py` governing all model calls with in-flight cap, sliding-window RPM/TPM tracking, deadline-aware 429 shedding, shared cooldowns, and step-isolated retries.
* **Why:**
  - When `retry_after > remaining_deadline`, waiting or retrying wastes tokens on an abandoned user. Shedding immediately (1.3 ms) preserves quota.
  - Shared cooldown ensures concurrent workers do not stampede the provider following a 429.
  - Step-isolated retries prevent redundant re-execution of earlier steps (understand, retrieve).
  - Multi-stage degradation (skipping query rewrite, reducing chunk count, skipping LLM verify) keeps throughput high during traffic bursts.
* **What it costs:** Degraded stages slightly reduce prompt refinement or evidence breadth during peak load to protect system availability.

---

### 2026-11-14: Controllable Stand-in Model Service
* **Decision:** Built `standin.py` as an `aiohttp` web server mimicking the `/v1/chat/completions` SSE streaming API.
* **Why:** Provides a sandbox for load shapes that would otherwise exhaust Groq free tier quotas (sustained overload, halving limits, 3pm daily budget exhaustion). Exposes live knobs via `/admin/config`, `/admin/reset`, and `/admin/exhaust_day`.
* **What it costs:** Default knobs contain `None` marked `# CALIBRATE` until filled with measurements from the baseline probe run.

---

### 2026-11-14: Empirical Calibration & Stand-in Validation (`calibrate.py` & `validate_standin.py`)
* **Decision:** Fitted stand-in simulator knobs directly from empirical measurements in `data/raw_real.jsonl`:
  - TTFT modeled via lognormal distribution ($\mu = 6.4399, \sigma = 0.3396$) with empirical median $587.64\text{ ms}$, empirical p95 $1408.76\text{ ms}$, and an $8\%$ bimodal outlier multiplier ($2.16\times$).
  - Per-token generation latency set to median $0.9247\text{ ms/token}$ ($\approx 1081\text{ tokens/s}$).
  - Concurrency slowdown modeled at $+25.0\text{ ms}$ per in-flight request, with an in-flight cap of 6 concurrent requests.
  - Rate limits set to 30 RPM, 8,000 TPM, and 429 payload matching Groq's exact schema with `retry-after: 2`.
* **Why:** Guarantees that the stand-in simulator accurately mirrors the physical timing characteristics, tail risks, and error semantics of Groq's `openai/gpt-oss-20b` endpoint without guessing parameters.
* **What it costs:** The local mock captures smooth stochastic distributions and step-function multipliers, but lacks the public internet network jitter (TLS/routing) and fine-grained hardware LPU micro-bursting dynamics of the real service.

---

### 2026-11-14: Workload Runner & Scenario Driving (`load_runner.py`)
* **Decision:** Implemented `load_runner.py` configured via `scenarios.yaml` to run the 5 specified traffic scenarios (`steady`, `burst`, `sustained_overload`, `rpm_halved`, `daily_budget_exhausted`).
  - Implemented Poisson arrival scheduling ($\Delta t \sim \text{Exp}(\lambda)$) for steady and overload conditions.
  - Enforced strict user patience limits ($T_{\text{patience}}$); requests exceeding patience terminate with `status="timeout"`.
  - Added strict safety gates for `--real` runs: requires an explicit `--cap <= 30` flag to strictly prevent accidental provider quota exhaustion.
  - Formatted output rows strictly matching the brief's required 12 CSV columns in `results.csv`, paired with detailed step execution traces in `traces.jsonl` and full answers in `answers.jsonl`.
* **Why:** Standardizes benchmarking across varying traffic profiles and guarantees complete, reproducible data collection for Table 2a and Table 2b.
* **What it costs:** High concurrency runs against the stand-in trigger local CPU context switching and rate-limit shedding as designed.
