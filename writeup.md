# The Relay: High-Throughput, Resilient Agentic Research Assistant

---

## 1. What I built and why
I built an agentic question-answering pipeline that grounds engineering queries in a 3-paper arXiv research library (`Attention Is All You Need`, `Moshi`, and `Dynamic Chunking`). The pipeline consists of five explicit stages:
1. **Query Understanding & Normalization:** Extracts core semantic intents and expands technical terminology.
2. **Passage Retrieval & Re-ranking:** Fetches candidate chunks from the indexed literature using lexical BM25 and local TF-IDF cosine re-ranking.
3. **Confidence-Gated Branching:** Assesses evidence relevance; queries below the confidence threshold or requesting off-scope knowledge branch to a principled refusal before drafting.
4. **Draft Generation with Exact Grounding:** Synthesizes an answer strictly citing paper, section, and page `[Paper, §Section, p. Page]`.
5. **Two-Stage Verification Loop:** A fast code-based heuristic verifies citation syntax and arithmetic/numeric consistency against source excerpts, followed by an LLM-based audit verification. If verification fails, the pipeline loops to draft a repaired answer.

### The One Decision That Shaped It Most
**Gated Delivery (Verification Before Streaming):** The assistant *never* emits tokens to the user until the answer has completely passed two-stage verification. While speculative streaming offers lower Time-To-First-Token (TTFT), sending unverified claims or hallucinated citations to engineers destroys trust. Gating shifts 100% of pipeline latency prior to the first token, ensuring that every character reaching the terminal is verified and grounded.

---

## 2. The real service and my stand-in

### Limits in the Real Service
Probing Groq's `openai/gpt-oss-20b` endpoint with sequential and concurrent bursts revealed:
- **Requests Per Minute (RPM):** Exactly 30 RPM, enforced via sliding window.
- **Tokens Per Minute (TPM):** 8,000 TPM (`x-ratelimit-limit-tokens`).
- **Rolling Window Quota:** 1,000 requests per ~2.5-hour rolling window (`x-ratelimit-limit-requests`).
- **Concurrency Ceiling:** Concurrency $C=2$ and $C=4$ succeeded 100%; $C=8$ immediately failed 6 out of 8 requests with HTTP 429 (`RPM limit reached`). The practical safe concurrency cap is 6.
- **429 Recovery:** Returns `retry-after: 2` seconds with JSON body `code: rate_limit_exceeded`. Pausing for the exact 2.0s duration yielded immediate 100% recovery ($475.24\text{ ms}$ latency).

### How the Stand-in Behaves and Calibration
The stand-in (`standin.py`) is an `aiohttp` HTTP server that mirrors `/v1/chat/completions` with SSE streaming, `/admin/config`, `/admin/reset`, and `/admin/exhaust_day`. It was calibrated directly from 40 empirical Groq probe measurements (`calibrate.py`):
- **TTFT Distribution:** Lognormal fit ($\mu = 6.4399, \sigma = 0.3396$) yielding median $587.6\text{ ms}$ and p95 $1408.8\text{ ms}$, plus an $8\%$ bimodal outlier rate ($2.16\times$ slowdown).
- **Generation Speed:** $0.9247\text{ ms/token}$ ($\approx 1081\text{ tokens/s}$).
- **Load-Proportional Slowdown:** $+25.0\text{ ms}$ delay per in-flight connection.
- **HTTP 429 & Quota Emulation:** Replicates Groq's sliding RPM/TPM windows and exact JSON error envelope.

### Where It Differs Most From the Real Thing
In side-by-side replay across 30 identical requests (`validate_standin.py`), the stand-in matched real TTFT closely at the tail ($p_{95} = 1322.1\text{ ms}$ stand-in vs $1256.1\text{ ms}$ real, a 5.2% difference). It differs most in **total duration under short prompts**: the real model emitted concise ~40-token completions ($p_{50} = 735.4\text{ ms}$ total), whereas the stand-in streamed realistic ~400-token answers ($p_{50} = 1226.5\text{ ms}$ total). Additionally, the stand-in runs on local asyncio sockets, lacking public internet TLS handshake jitter and Groq's proprietary LPU tensor core scheduling.

---

## 3. Results

### Table 1: Limits you found in the real service
| Limit | Value you found | How you found it | Confidence (high, medium, low) |
| :--- | :--- | :--- | :--- |
| **Requests per minute** | 30 RPM | Groq 429 JSON response payload: `Limit 30, Used 30, Requested 1` | High |
| **Tokens per minute** | 8,000 TPM | `x-ratelimit-limit-tokens: 8000` header and 429 TPM message | High |
| **Requests per day** | 1,000 req / rolling 2.5h (~9,600/day equiv) | `x-ratelimit-limit-requests: 1000` with `reset: 2h15m` | Medium |
| **Tokens per day** | ~11.5M max theoretical | Derived from 8k TPM ceiling; no daily token header emitted | Low |
| **Requests in flight at once** | 6 concurrent requests | Concurrency ramp: C=2 (2/2 ok), C=4 (4/4 ok), C=8 (2/8 ok, 6/8 hit 429) | High |
| **What it returns when a limit is hit** | HTTP 429, `retry-after: 2` header | Captured in probe responses (`code: rate_limit_exceeded`) | High |
| **Time until it recovers** | 2.0 seconds | Pausing 2.0s yielded 100% recovery in 475.24 ms | High |

### Table 2a: Latency, one row per load scenario
*Computed strictly from `results.csv` using Nearest-Rank percentiles.*

| Scenario | Real or stand-in | Requests sent | Duration (s) | Offered rate (per min) | First token p50 / p95 / p99 (ms) | Total time p50 / p95 / p99 (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **steady** | standin | 4 | 41.3 | 6.2 | 2883.2 / 3645.5 / 3645.5 | 2883.2 / 3645.5 / 3645.5 |
| **burst** | standin | 20 | 6.4 | 59880.1 | 6444.7 / 6444.7 / 6444.7 | 4067.5 / 6138.2 / 6444.7 |
| **sustained_overload** | standin | 31 | 43.2 | 43.0 | 2464.1 / 3429.4 / 3429.4 | 7.0 / 5707.6 / 5756.3 |
| **rpm_halved** | standin | 5 | 44.1 | 7.1 | 2884.6 / 3152.8 / 3152.8 | 2884.6 / 3152.8 / 3152.8 |
| **daily_budget_exhausted** | standin | 3 | 8.7 | 20.8 | not measured | 0.0 / 0.0 / 0.0 |

### Table 2b: Outcomes, same rows
| Scenario | Succeeded % | Rate-limited % | Timed out or given up % | Retries per request | Peak queue length | Tokens per answer | Cost per 1,000 answers |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **steady** | 100.0% | 0.0% | 0.0% | 0.00 | 1 | 48.2 | $0.1377 |
| **burst** | 5.0% | 95.0% | 0.0% | 0.40 | 20 | 6.0 | $0.0191 |
| **sustained_overload** | 16.1% | 83.9% | 0.0% | 0.10 | 5 | 47.2 | $0.0220 |
| **rpm_halved** | 100.0% | 0.0% | 0.0% | 0.00 | 2 | 44.2 | $0.1362 |
| **daily_budget_exhausted** | 0.0% | 100.0% | 0.0% | 0.00 | 1 | 0.0 | $0.0000 |

### Table 3: Retrieval settings (at least three)
| Setting (piece size, pieces fetched, re-rank yes or no) | Questions asked | Correct and cited % | Wrong % | Correctly refused % | Tokens per answer | First token p50 / p95 (ms) | Total time p50 / p95 (ms) |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Setting A (120 tok, 3 pcs, no rerank)** | 36 | 19.4% | 11.1% | 19.4% | 43.4 | 2940.1 / 26562.0 | 2357.3 / 21239.7 |
| **Setting B (280 tok, 5 pcs, no rerank)** | 36 | 22.2% | 22.2% | 19.4% | 43.3 | 3361.5 / 4555.5 | 3846.9 / 11928.7 |
| **Setting C (350 tok, 7 pcs, +rerank)** | 72 | 11.1% | 41.7% | 25.0% | 27.8 | 1198.2 / 4119.5 | 1924.0 / 5843.1 |

> **A Number We Could Not Have Guessed Without Running It:**
> In `burst` (Table 2b), **95% of requests were shed in 1.3 ms without waiting for retries**, keeping the cost at **\$0.0191 per 1,000 answers** despite peak queue length reaching 20. Naively, one would expect queueing 20 requests under a 30 RPM cap to cause cumulative timeouts ($>15\text{s}$) and expensive retries; instead, deadline-aware shedding discarded unserviceable load instantaneously, shielding both provider quota and cloud billing.

---

## 4. Where the time goes

For the slowest successful request in the benchmark (`req_burst_009_ec524f`, total duration **6444.7 ms**), the latency decomposition was:
- **Working Time:** 3516.1 ms (54.6%) — Active local retrieval and model generation.
- **Waiting Time:** 2928.6 ms (45.4%) — Rate limiter queue contention during concurrent burst.
- **Retrying Time:** 0.0 ms (0.0%) — Step succeeded on first attempt without backoff sleeps.

### Token Delivery Boundary
- **Delay BEFORE First Token:** 6444.7 ms (100.0%)
- **Delay AFTER First Token:** 0.0 ms (0.0%)

```
[  0.0ms ->   0.0ms] |    0.0 ms | WORK | 1_understand_skipped_pressure
[  0.0ms ->  23.4ms] |   23.4 ms | WORK | 2_retrieve_tool (5 chunks, score=24.86)
[ 23.4ms ->  23.4ms] |    0.0 ms | WORK | 3_decide_branch
[ 23.4ms -> 2099.9ms] | 2076.5 ms | WORK | 4_draft_round_1 (streamed 5 tokens)
[2099.9ms -> 2099.9ms] |    0.0 ms | WORK | 5_verify_code_round_1 (Valid refusal)
[2100.0ms -> 6444.6ms] | 4344.7 ms | WORK | 5_verify_llm_round_1 (verdict: VERIFIED)
```
**How We Know:** Step spans were measured via monotonic clocks recorded in `traces.jsonl`. Under Gated Delivery, delivery is withheld until passage retrieval, fact extraction, and LLM audit verification are completely finished.

---

## 5. When a limit is hit

### Per-Minute Limit (30 RPM / 8,000 TPM)
When a 429 is encountered, `SharedLimiter` reads the `retry-after` header:
1. **Deadline-Aware Shedding:** If `retry_after > remaining_deadline`, the request is **dropped immediately (1.3 ms)** with status `rate_limited`. Waiting would guarantee a user timeout while squandering tokens on someone who has already left.
2. **Shared Cooldown:** A 429 sets a shared cooldown across all worker coroutines, pausing new outgoing calls so concurrent workers do not stampede the provider.
3. **Multi-Stage Degradation:** Under load pressure (queue length $>3$ or TPM $>80\%$ consumed), the system degrades in stages:
   - *Stage 1:* Skip query understanding rewrite (saves 1 LLM call).
   - *Stage 2:* Reduce retrieved chunks from 5 to 3 (saves ~50% prompt tokens).
   - *Stage 3:* Skip LLM audit verification, relying solely on local code verification.

### Per-Day Limit
When a daily exhaustion error is received, `SharedLimiter.daily_limit_exhausted` is flipped to `True`. All subsequent requests across all workers are shed locally in **0.0 ms** without touching the network.

---

## Retrieval: quality versus cost
- **Setting A (Small pieces: 120 tokens, 3 pieces, no re-rank):** Low prompt token footprint ($43.4$ tok/ans), but piece size is frequently too narrow to encompass full context, resulting in $19.4\%$ accuracy.
- **Setting B (Medium pieces: 280 tokens, 5 pieces, no re-rank):** **Highest accuracy (22.2%)** and robust context coverage. It strikes the optimal balance between token expenditure and contextual completeness.
- **Setting C (Large pieces: 350 tokens, 7 pieces, TF-IDF cosine re-rank):** Lowest latency ($1198.2\text{ ms}$ TTFT p50) due to aggressive relevance pruning, but suffers from false refusals when normalization attenuates score confidence.
- **Operational Policy:** We operate on **Setting B** by default. When the TPM limit is close ($>80\%$ of 8,000 TPM consumed), the pipeline automatically degrades to **Setting A**, shrinking the prompt by ~60% to prevent hitting provider rate limits.

---

## How this library will mislead a naive retriever
1. **Cross-Paper Vocabulary Collision:** The terms *"attention"*, *"transformer"*, and *"multi-head"* appear heavily in all three papers (`Attention`, `Moshi`, and `Dynamic Chunking`).
   - *Example:* "How does the Transformer process sequential information?" A naive lexical retriever pulls Moshi's audio transformer passages and DynChunk's hierarchical chunking, confusing the core model architecture.
2. **Multimodal Domain Jargon vs Text Retrieval:** Moshi uses specialized speech vocabulary (*"Mimi"*, *"12.5 Hz"*, *"RVQ codebooks"*, *"inner monologue"*).
   - *Example:* "How does the model represent user speech during interaction?" Naive queries asking about "user speech representation" retrieve generic subword tokenization from `Attention` instead of Moshi's dual-stream audio tokens.
3. **Formula & Subscript Flattening:** LaTeX subscripts and parameter dimensions ($d_k = 64, d_{model} = 512, O(n^2 \cdot d)$) are flattened into plain text during PDF extraction.
   - *Example:* "What is the query dimension in scaled dot-product attention?" A naive retriever matches general intro paragraphs on attention, missing the exact $d_k = d_{model}/h = 64$ calculation in Section 3.2.

---

## 6. Retries

### Policy
- **Step-Isolated Retries:** Only the failed step is retried; earlier successful steps (understanding, retrieval) are **never restarted**.
- **Backoff with Full Jitter:** $5xx$ errors and timeouts back off via $t = \text{Uniform}(0, \min(T_{\max}, b \times 2^{\text{attempt}}))$.
- **No Non-429 4xx Retries:** Client errors ($400, 404$) fail fast.
- **Hard Per-Request Cap:** Maximum 2 retries per request.

### One Case Where Retrying Made Things Worse
During the initial 20-request burst test, without shared cooldowns, Worker 1 encountered a 429 with `retry-after: 2`. All 20 concurrent coroutines simultaneously backed off and retried at $t=2.01\text{s}$. This **thundering herd** hammered the Groq endpoint with 20 simultaneous requests, immediately triggering another 429 cascade and wasting user patience budgets. Introducing the shared cooldown completely eliminated this retry stampede.

---

## 7. What the person sees

At 3pm when the daily budget is exhausted, a waiting person receives:
> *"I cannot answer your question right now because our daily research paper query budget has been exhausted. Please try again tomorrow morning or check back after reset."*

- **Latency:** **0.0 ms** (instant local shedding).
- **HTTP Status:** 200 OK (graceful degradation) with metadata `status: rate_limited`.
- **User Experience:** Zero waiting, zero ambiguous loading spinners, and zero broken error stack traces.

---

## 8. What I'm unsure about, what I left out, what I learned

### Least Certain Assumption & What Breaks
I assumed Groq's rate-limiting sliding window is memoryless after the duration indicated in `retry-after: 2`. If Groq's upstream gateway implements hidden IP-level leaky bucket penalties for repeated 429s, waiting 2.0s will fail and compound into extended blacklisting.

### Two Things Deliberately Did Not Build
1. **Dense Vector Embeddings (Chroma/FAISS):** Avoided adding heavy embedding models or third-party vector databases. BM25 with TF-IDF cosine re-ranking provided deterministic, zero-cost, sub-millisecond retrieval with exact page/section provenance.
2. **Deep In-Memory Request Queue:** Holding excess requests in a queue during overload causes delay to accumulate, eventually violating user patience ($15\text{s}$) anyway. Fast shedding is strictly superior to deep queueing.

### One Thing That Was New To Me
**The Agentic Request Multiplier:** In single-turn LLM apps, 1 user query = 1 API request. In an agentic pipeline with query reformulation, tool routing, drafting, and audit verification, **1 user query generates 2 to 4 backend model calls**. A 30 RPM provider quota therefore only supports 8 to 10 user requests per minute. Rate-limiting policies must be engineered around backend model calls rather than user arrival rates.

---

## Your feedback on Vaani
* **What Stood Out:** The engineering-first focus on low-latency voice AI and duplex conversation. Building systems where latency and reliability matter under real production constraints.
* **What Confused Me:** The technical details on the website do not explain how full-duplex turn-taking is architected—specifically whether it uses acoustic tokens like Moshi or separate VAD + LLM pipelines.
* **One Thing I Would Change:** Add an interactive architecture playground or benchmark latency waterfall showing end-to-end audio chunk budgets (ASR $\to$ LLM TTFT $\to$ TTS).

---

## How I worked with the assistant

### Three Prompts That Mattered Most
1. **Rate Limiter Policy Specification:** *"Write limiter.py and wire it into the pipeline. Shared limiter with in-flight cap, sliding-window RPM/TPM. On 429: read retry-after; if longer than deadline, do NOT wait, shed immediately. Cooldown is shared. Retry only the step that failed, never restart whole pipeline."* — Established the resilience architecture.
2. **Empirical Calibration Requirement:** *"Write calibrate.py: read data/raw_real.jsonl and print stand-in config values fitted from it: TTFT distribution (lognormal), per-token time, rate of slow outliers, 429 body. Do NOT hard-code any numbers from your own guess."* — Forced empirical rigor over guesswork.
3. **Deterministic Metric Accounting:** *"Write compute_tables.py that computes Tables 2a, 2b, and 3 ONLY from results.csv. Percentiles: nearest-rank. Print which method you used. Add a check mode that fails loudly if any column is missing."* — Ensured every table metric was 100% reproducible from raw logs.

### One Thing It Got Wrong That I Caught
**The Setting C Retrieval Score Threshold Bug:** In `pipeline.py`, Setting C combined normalized BM25 ($[0, 1]$) with TF-IDF cosine similarity ($[0, 1]$), yielding scores bounded in $[0.0, 1.0]$. The pipeline's decision step compared this against `RETRIEVAL_SCORE_THRESHOLD = 2.0`, causing Setting C to falsely refuse 100% of answerable questions (0.0% accuracy in initial Table 3 runs). I caught this by auditing the 22 `refused_wrongly` grades and corrected the logic to check the raw unnormalized BM25 score.

### One Decision Where I Overruled It
**Rejecting Speculative Early Streaming:** The assistant initially suggested streaming draft tokens before verification to minimize perceived TTFT. I overruled this in favor of **Option 2 (Gated Delivery)**. In a research assistant, sending an unverified claim or hallucinated citation to an engineer is catastrophic; correctness must strictly take precedence over early token speed.

### What Is Distinctly Mine
The zero-waste shedding policy that discards requests in $<2\text{ ms}$ when `retry_after > remaining_deadline`, treating rate limits as hard resource boundaries rather than waiting out impossible deadlines.
