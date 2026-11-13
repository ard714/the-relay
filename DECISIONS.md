# Architectural & Engineering Decisions Log

This document tracks all key technical choices, trade-offs, rationale, and associated costs as mandated by the project constraints.

---

### 2026-11-14: Model Selection for Baseline Calibration
* **Decision:** Selected `openai/gpt-oss-20b` on Groq (confirmed by user) after verifying that `llama-3.1-8b-instant` returned 404 (unavailable on Groq endpoint).
* **Why:** High throughput, 1,000 RPM / 8,000 TPM free tier limits, fast TTFT (~70-100ms), and full compatibility with OpenAI-compatible chat completion payload.
* **What it costs:** 20B parameters; balances speed against the reasoning depth of a 120B/70B model.

---

### 2026-11-14: Document Ingestion and Heading Tracking
* **Decision:** Parse the 3 arXiv papers with PyMuPDF (`fitz`), tracking the active section heading in force at the top of each page using a hybrid heuristic (font size >= 1.12x body font, bold flags, and regex for standard/numbered headings).
* **Why:** Enables precise paper/page/section attribution for every retrieved passage without brittle OCR or opaque external parsers.
* **What it costs:** Requires downloading the PyMuPDF native wheel and managing PDF layout edge cases (e.g. multi-column text, formulas).


---

### 2026-11-14: Stand-in Architecture
* **Decision:** Implement the stand-in model service as a standalone local HTTP server (FastAPI/Uvicorn) with an async simulator core.
* **Why:** Enforces true protocol and transport parity with Groq over `httpx`, subjecting the client pipeline to real socket connection pooling, HTTP headers, chunked SSE streaming, and network timeouts.
* **What it costs:** Requires managing a local background server process and local port binding during testing rather than an in-memory mock.

---

### 2026-11-14: Retrieval Strategy Phase 1
* **Decision:** Implement pure BM25 lexical passage retrieval over chunked arXiv papers for the initial pipeline baseline, before testing dense embeddings.
* **Why:** Fast, lightweight, zero-dependency token matching, highly effective for exact keyword and terminology lookups in research papers without external API overhead.
* **What it costs:** Lacks semantic generalization for questions phrased in non-exact synonyms or paraphrased queries.

---

### 2026-11-14: Evaluation Harness
* **Decision:** Build an automated evaluation and scoring harness for the 15+ benchmark questions that assigns preliminary grades (`correct`, `wrong`, `refused_correctly`, `refused_wrongly`) alongside ground-truth answer verification.
* **Why:** Enables repeatable, automated sweeps across multiple load profiles and retrieval configurations.
* **What it costs:** Automated grading heuristics require rigorous spot-checking to avoid false positives/negatives in citation matching.

