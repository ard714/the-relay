# Architectural & Engineering Decisions Log

This document tracks all key technical choices, trade-offs, rationale, and associated costs as mandated by the project constraints.

---

### 2026-11-14: Model Selection for Baseline Calibration
* **Decision:** Selected `openai/gpt-oss-20b` on Groq (confirmed by user) after verifying that `llama-3.1-8b-instant` returned 404 (unavailable on Groq endpoint).
* **Why:** High throughput, 1,000 RPM / 8,000 TPM free tier limits, fast TTFT (~70-100ms), and full compatibility with OpenAI-compatible chat completion payload.
* **What it costs:** 20B parameters; balances speed against the reasoning depth of a 120B/70B model.

---

### 2026-11-14: Stand-in Architecture
* **Decision:** Implement the stand-in model service as a standalone local HTTP server (FastAPI/Uvicorn) with an async simulator core.
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
