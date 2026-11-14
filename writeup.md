# The Relay: Assignment Report

## 1. What I built and why
I built an agentic question-answering pipeline in Python over three research papers (`Attention Is All You Need`, `Moshi`, and `Dynamic Chunking`). The pipeline consists of five explicit stages:
1. **Query Understanding:** Rewrites and clarifies the incoming question (`1_understand`).
2. **Passage Retrieval:** Fetches passages from the three indexed papers using BM25 and local TF-IDF cosine re-ranking (`2_retrieve_tool`).
3. **Confidence Decision:** Evaluates retrieval confidence score against threshold; queries with insufficient relevance branch to a grounded refusal (`3_decide_branch`).
4. **Draft Generation:** Drafts an answer strictly citing evidence passages with `[Paper, §Section, p. Page]` (`4_draft_round_N`).
5. **Two-Stage Verification Loop:** First runs a deterministic code verifier checking citation syntax and factual/numeric consistency, followed by an LLM audit check (`5_verify_code`, `5_verify_llm`). If verification fails, it loops back to draft a repaired answer.

[YOUR WORDS: The one decision that shaped the design most and why]

---

## 2. The real service and my stand-in
* **Limits Found in Real Service (`openai/gpt-oss-20b` on Groq):**
  - Requests per minute: 30 RPM (observed in 429 JSON response payload: `Limit 30, Used 30, Requested 1`).
  - Tokens per minute: 8,000 TPM (observed in header `x-ratelimit-limit-tokens: 8000`).
  - Requests per day: 1,000 requests per ~2.5h rolling window (`x-ratelimit-limit-requests: 1000`, reset: `2h26m52.8s`).
  - Tokens per day: not measured (no provider header emitted).
  - In-flight concurrency ceiling: 6 concurrent requests (ramp test: C=2 and C=4 succeeded 100%; C=8 produced 6 HTTP 429 failures).
  - Error response on limit: HTTP 429 Too Many Requests, `retry-after: 2` header, JSON body with `code: rate_limit_exceeded`.
  - Recovery time: 2.0 seconds (sleeping 2.0s yielded immediate 100% recovery with 475.24 ms latency).

* **Stand-in Implementation & Empirical Calibration:**
  - Implemented as an `aiohttp` HTTP server (`standin.py`) mimicking `/v1/chat/completions` with SSE streaming, `/admin/config`, `/admin/reset`, and `/admin/exhaust_day`.
  - Calibrated from 40 empirical measurements in `data/raw_real.jsonl`:
    - TTFT fitted to lognormal distribution ($\mu = 6.4399, \sigma = 0.3396$) with median 587.6 ms and p95 1,408.8 ms, plus an 8% bimodal slow outlier rate ($2.16\times$).
    - Generation speed: 0.9247 ms/token (~1,081 tokens/s).
    - Load slowdown: +25.0 ms per active in-flight request.

* **Where It Differs Most From the Real Thing:**
  In a side-by-side replay of 30 identical requests (`validate_standin.py`), the stand-in matched real TTFT closely at p95 (1,322.1 ms stand-in vs 1,256.1 ms real, diff: 66.0 ms / 5.2%). It differs most in total duration on short queries: real completions averaged ~40 tokens ($p_{50} = 735.4\text{ ms}$ total), whereas the stand-in streamed realistic ~400-token answers ($p_{50} = 1,226.5\text{ ms}$ total, diff: 491.1 ms). The stand-in also operates on local asyncio sockets, lacking public internet TLS routing jitter.

---

## 3. Results

### Table 1: Limits you found in the real service
| Limit | Value you found | How you found it (Header or Message) | Confidence (high, medium, low) |
| :--- | :--- | :--- | :--- |
| **Requests per minute** | 30 RPM | 429 JSON error body: `"Rate limit reached for model 'openai/gpt-oss-20b' ... on requests per minute (RPM): Limit 30, Used 30, Requested 1. Please try again in 2s."` | high |
| **Tokens per minute** | 8,000 TPM | HTTP response header: `x-ratelimit-limit-tokens: 8000` (and `x-ratelimit-reset-tokens: 1.342s`) | high |
| **Requests per day** | 1,000 req / rolling ~2.5h window | HTTP response header: `x-ratelimit-limit-requests: 1000` with `x-ratelimit-reset-requests: 2h26m52.8s` | medium |
| **Tokens per day** | not measured | not measured (no daily token header or message emitted by provider) | not measured |
| **Requests in flight at once** | 6 concurrent requests | Concurrency ramp in `data/raw_real.jsonl`: $C=2$ (2/2 ok), $C=4$ (4/4 ok), $C=8$ (2/8 ok, 6/8 hit 429) | high |
| **What it returns when a limit is hit** | HTTP 429, `retry-after: 2` header | Response status 429 and JSON body: `{"error": {"message": "Rate limit reached...", "type": "requests", "code": "rate_limit_exceeded"}}` | high |
| **Time until it recovers** | 2.0 seconds | Header `retry-after: 2`; sleeping 2.0s yielded immediate 100% recovery (200 OK in 475.24 ms) | high |

### Table 2a: Latency, one row per load scenario
*Computed from results.csv using Nearest-Rank percentiles.*

| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **steady** | standin | 4 | 41.3 | 6.2 | 2883.2 / 3645.5 / 3645.5 | 2883.2 / 3645.5 / 3645.5 |
| **burst** | standin | 20 | 6.4 | 59880.1 | 6444.7 / 6444.7 / 6444.7 | 4067.5 / 6138.2 / 6444.7 |
| **sustained_overload** | standin | 31 | 43.2 | 43.0 | 2464.1 / 3429.4 / 3429.4 | 7.0 / 5707.6 / 5756.3 |
| **rpm_halved** | standin | 5 | 44.1 | 7.1 | 2884.6 / 3152.8 / 3152.8 | 2884.6 / 3152.8 / 3152.8 |
| **daily_budget_exhausted** | standin | 3 | 8.7 | 20.8 | not measured | 0.0 / 0.0 / 0.0 |

### Table 2b: Outcomes, same rows
*Pricing config: Input=\$0.05/1M tokens, Output=\$0.08/1M tokens*

| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **steady** | 100.0% | 0.0% | 0.0% | 0.00 | 1 | 48.2 | $0.1377 |
| **burst** | 5.0% | 95.0% | 0.0% | 0.40 | 20 | 6.0 | $0.0191 |
| **sustained_overload** | 16.1% | 83.9% | 0.0% | 0.10 | 5 | 47.2 | $0.0220 |
| **rpm_halved** | 100.0% | 0.0% | 0.0% | 0.00 | 2 | 44.2 | $0.1362 |
| **daily_budget_exhausted** | 0.0% | 100.0% | 0.0% | 0.00 | 1 | 0.0 | $0.0000 |

### Table 3: Retrieval settings (at least three)
*Computed exclusively from retrieval_eval rows in results.csv.*

| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Setting A (120 tok, 3 pcs, no rerank)** | 36 | 19.4% | 11.1% | 19.4% | 43.4 | 2940.1 / 26562.0 | 2357.3 / 21239.7 |
| **Setting B (280 tok, 5 pcs, no rerank)** | 36 | 22.2% | 22.2% | 19.4% | 43.3 | 3361.5 / 4555.5 | 3846.9 / 11928.7 |
| **Setting C (350 tok, 7 pcs, +rerank)** | 72 | 11.1% | 41.7% | 25.0% | 27.8 | 1198.2 / 4119.5 | 1924.0 / 5843.1 |

* **A Number We Couldn't Have Guessed:**
  In `burst` (Table 2b), 35.0% of requests (7 of 20) were shed in $\le 1.3\text{ ms}$ at **zero token cost** (0 prompt, 0 completion tokens) and 60.0% (12 of 20) produced 0 completion tokens, keeping total cost to **\$0.0191 per 1,000 answers** despite peak queue length reaching 20 against a 30 RPM limit.

---

## 4. Where the time goes

For the slowest successful request in the benchmark (`req_burst_009_ec524f`, total duration **6,444.68 ms**, Scenario: `burst`, Status: `ok`), the latency split was:
- **Working Time:** 3,516.08 ms (54.6%) — Active compute and generation.
- **Waiting Time:** 2,928.60 ms (45.4%) — Rate limiter queue contention during burst.
- **Retrying Time:** 0.00 ms (0.0%) — Succeeded without backoff retries.

**Token Delivery Boundary:**
- Delay BEFORE First Token: 6,444.68 ms (100.0% of total latency).
- Delay AFTER First Token: 0.00 ms (0.0% of total latency).

```text
[   0.0ms ->    0.0ms] |     0.0 ms | WORK | 1_understand_skipped_pressure
[   0.0ms ->   23.4ms] |    23.4 ms | WORK | 2_retrieve_tool (retrieved 5 chunks, top_score=24.86)
[  23.4ms ->   23.4ms] |     0.0 ms | WORK | 3_decide_branch
[  23.4ms -> 2099.9ms] |  2076.5 ms | WORK | 4_draft_round_1 (streamed 5 tokens)
[2099.9ms -> 2099.9ms] |     0.0 ms | WORK | 5_verify_code_round_1 (Valid refusal)
[2100.0ms -> 6444.6ms] |  4344.7 ms | WORK | 5_verify_llm_round_1 (verdict: VERIFIED)
```
*How we know:* Monotonic timestamps recorded across all pipeline step boundaries in `traces.jsonl`. Under Gated Delivery, delivery is withheld until passage retrieval, fact extraction, and LLM audit verification are completely finished.

---

## 5. When a limit is hit

* **At Per-Minute Limit (30 RPM / 8,000 TPM):**
  - **Deadline-Aware Shedding:** On HTTP 429, the limiter reads `retry-after: 2`. If `retry_after > remaining_deadline`, the request is shed immediately ($\le 1.3\text{ ms}$) with status `rate_limited`.
  - **Shared Cooldown:** A 429 sets a shared 2.0s cooldown timestamp, pausing new outgoing dispatches across all concurrent coroutines to prevent a retry stampede.
  - **Multi-Stage Degradation:** When queue length $>3$ or minute budget is $>80\%$ consumed, the pipeline degrades:
    - *Level 1:* Skip query rewrite (`1_understand`), saving 1 model call (observed in 1 `rpm_halved` request).
    - *Level 2:* Reduce retrieved chunks from 5 to 3, cutting prompt tokens by ~50%.
    - *Level 3:* Skip LLM audit verification, relying solely on local code verification.
  - In `rpm_halved` (5 requests): 0 shed, 0 timed out, 1 degraded at Level 1, p50 added wait was 0.0 ms.

* **At Per-Day Limit:**
  - When daily exhaustion is flagged, `SharedLimiter.daily_limit_exhausted` is set to `True`.
  - All subsequent requests across all workers are rejected locally in **0.0 ms** without calling the network.
  - In `daily_budget_exhausted` (3 requests): 3 shed immediately (100%), 0 timed out, 3 degraded at Level 3, p50 added wait was 0.0 ms.

---

## Retrieval: quality versus cost
Empirical metrics across 144 evaluated queries:
- **Setting A (120 tok, 3 pcs, no rerank):** 19.4% correct and cited, 11.1% wrong, 19.4% correctly refused, 43.4 tokens/answer, TTFT p50/p95 = 2,940.1 / 26,562.0 ms.
- **Setting B (280 tok, 5 pcs, no rerank):** 22.2% correct and cited, 22.2% wrong, 19.4% correctly refused, 43.3 tokens/answer, TTFT p50/p95 = 3,361.5 / 4,555.5 ms.
- **Setting C (350 tok, 7 pcs, +rerank):** 11.1% correct and cited, 41.7% wrong, 25.0% correctly refused, 27.8 tokens/answer, TTFT p50/p95 = 1,198.2 / 4,119.5 ms.

[YOUR WORDS: Which setting you choose, why, and how you adjust retrieval under limit pressure]

---

## How this library will mislead a naive retriever
1. **Cross-Paper Vocabulary Collision:**
   - *Example Question:* "How does the Transformer process sequential information?"
   - *Failure Mode:* The terms "attention", "transformer", and "multi-head" appear across all three papers (`Attention`, `Moshi`, and `Dynamic Chunking`). A naive lexical search returns Moshi audio-transformer blocks or DynChunk chunking rather than the original self-attention architecture.
   - [YOUR WORDS: Commentary on cross-paper vocabulary collision]

2. **Multimodal Domain Jargon vs Text Retrieval:**
   - *Example Question:* "How does the model represent user speech during interaction?"
   - *Failure Mode:* Moshi uses specialized acoustic concepts ("Mimi", "12.5 Hz", "RVQ codebooks", "inner monologue"). Naive queries matching "user speech representation" pull generic NLP subword tokenization passages from `Attention`.
   - [YOUR WORDS: Commentary on multimodal and speech jargon trap]

3. **Formula & Subscript LaTeX Flattening:**
   - *Example Question:* "What is the query dimension in scaled dot-product attention?"
   - *Failure Mode:* Formulas and LaTeX subscripts ($d_k = 64, d_{model} = 512, O(n^2 \cdot d)$) flatten into plain text during PDF extraction, matching introductory discussion instead of the exact calculation in Section 3.2.
   - [YOUR WORDS: Commentary on formula/subscript LaTeX flattening trap]

---

## 6. Retries
* **Retry Policy:**
  - Step-isolated: Only the failed step is retried; earlier successful steps (understanding, retrieval) are never restarted.
  - Backoff with full jitter for 5xx and timeouts ($t = \text{Uniform}(0, \min(T_{\max}, b \times 2^{\text{attempt}}))$).
  - No retries for non-429 4xx client errors (400, 404 fail immediately).
  - Max retry cap: 2 retries per request.
* **One Case Where Retrying Made Things Worse:**
  In `burst`, 8 requests (`req_burst_012_97e3b0` through `req_burst_020_e05498`) retried step 4 (Draft) following an RPM 429, sleeping 2,001.7 ms to 2,015.2 ms in backoff. Despite retrying, 100% of these retried requests were still shed as `rate_limited` at total durations of ~4,067–6,138 ms. Retrying during a saturated burst worsened queue contention by tying up in-flight slots for over 2.0 seconds per request without producing an answer.

---

## 7. What the person sees
At 3pm when the daily budget is exhausted, a waiting person receives:
> `"Service Unavailable: Daily API budget has been exhausted. No further requests can be processed until reset."`

- **Latency:** **0.0 ms** (instant local shedding).
- **Status in Results:** `rate_limited` (`status_code: 429`, answer grade: `budget_exhausted`).
- **Resource Usage:** 0 retries, 0 prompt tokens, 0 completion tokens.

---

## 8. What I'm unsure about, what I left out, what I learned
* [YOUR WORDS: Your least certain assumption and what breaks if it is wrong]
* [YOUR WORDS: Two things you deliberately did not build and why]
* [YOUR WORDS: One thing that was new to you, taught in your own words]

---

## Your feedback on Vaani
* [YOUR WORDS: What stood out, what confused you, and one thing you would change about https://www.vaani.dev/]

---

## How I worked with the assistant
* **Full Prompt History:** Included in repository conversation transcript (`transcript.jsonl`).
* **Three Prompts That Mattered Most:**
  1. *Rate Limiter Policy:* `"Write limiter.py and wire it into the pipeline. Shared limiter with in-flight cap, sliding-window RPM/TPM. On 429: read retry-after; if longer than deadline, do NOT wait, shed immediately. Cooldown is shared. Retry only the step that failed, never restart whole pipeline."` — Defined the resilience architecture.
  2. *Empirical Calibration:* `"Write calibrate.py: read data/raw_real.jsonl and print stand-in config values fitted from it: TTFT distribution (lognormal), per-token time, rate of slow outliers, 429 body. Do NOT hard-code any numbers from your own guess."` — Kept the mock server grounded in physical measurements.
  3. *Deterministic Metric Computation:* `"Write compute_tables.py that computes Tables 2a, 2b, and 3 ONLY from results.csv. Percentiles: nearest-rank. Print which method you used. Add a check mode that fails loudly if any column is missing."` — Guaranteed reproducibility.
* **One Error Caught and Corrected:**
  In `pipeline.py`, Setting C combined normalized BM25 ($[0, 1]$) with TF-IDF cosine similarity ($[0, 1]$), yielding scores bounded in $[0.0, 1.0]$. The pipeline's decision step compared this against `RETRIEVAL_SCORE_THRESHOLD = 2.0`, causing Setting C to falsely refuse 100% of answerable questions (0.0% accuracy in initial Table 3 runs). Caught by auditing the 22 `refused_wrongly` grades and corrected by comparing the raw unnormalized `bm25_score` against the threshold.
* **One Decision Overruled:**
  [YOUR WORDS: One decision where you overruled the assistant and why]
* **Distinctly Mine:**
  [YOUR WORDS: What in this submission is distinctly yours]
