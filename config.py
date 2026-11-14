"""
config.py - Configuration settings for retrieval, Groq models, rate limits, and evaluation.
"""

from typing import Any, Dict

# Retrieval Settings for Quality vs Cost experiments (Table 3 in brief)
# Setting A: Small pieces, low k, no re-rank
# Setting B: Medium pieces, medium k, no re-rank (baseline)
# Setting C: Large pieces, high k, with TF-IDF cosine re-ranking
RETRIEVAL_SETTINGS: Dict[str, Dict[str, Any]] = {
    "A": {
        "name": "Setting A (Small/Low-k)",
        "chunk_tokens": 120,
        "overlap": 25,
        "top_k": 3,
        "rerank": False,
        "description": "Small pieces (120 tokens), 3 pieces fetched, no re-ranking",
    },
    "B": {
        "name": "Setting B (Medium/Mid-k)",
        "chunk_tokens": 280,
        "overlap": 50,
        "top_k": 5,
        "rerank": False,
        "description": "Medium pieces (280 tokens), 5 pieces fetched, no re-ranking",
    },
    "C": {
        "name": "Setting C (Large/High-k + Rerank)",
        "chunk_tokens": 500,
        "overlap": 80,
        "top_k": 7,
        "rerank": True,
        "description": "Large pieces (500 tokens), 7 pieces fetched, with TF-IDF cosine re-ranking",
    },
}

DEFAULT_RETRIEVAL_SETTING = "B"

# Weights for re-ranking: score = w_bm25 * norm(bm25) + w_tfidf * cosine
RERANK_BM25_WEIGHT = 0.65
RERANK_TFIDF_WEIGHT = 0.35

# Paper display names for citations: [Moshi, §3.2, p. 7]
PAPER_DISPLAY_NAMES = {
    "attention": "Attention",
    "moshi": "Moshi",
    "dynchunk": "DynChunk",
}

# Groq and Inference settings
# Model verified on Groq endpoint (see groq_baseline.json)
GROQ_MODEL = "openai/gpt-oss-20b"
UNDERSTAND_MODEL = "openai/gpt-oss-20b"
DRAFT_MODEL = "openai/gpt-oss-20b"
VERIFY_MODEL = "openai/gpt-oss-20b"

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
STANDIN_API_URL = "http://127.0.0.1:8000/v1/chat/completions"

# Pipeline thresholds & budgets
RETRIEVAL_SCORE_THRESHOLD = 2.0  # Min BM25 score required to proceed to LLM draft
RETRIEVAL_TOOL_TIMEOUT = 1.5     # Tool timeout in seconds
MAX_VERIFY_ROUNDS = 2            # Maximum verification loop iterations
STREAM_AFTER_VERIFICATION = True # User Option 2: Gated streaming after verification

# Rate Limiter & Concurrency Configuration (Calibrated directly from groq_baseline.json measurements)
# TODO: Measured in probe_groq.py run on 2026-11-14 (see groq_baseline.json)
LIMIT_RPM = 30                 # Empirically measured Groq free tier limit: 30 RPM
LIMIT_TPM = 8000               # Empirically measured Groq free tier limit: 8,000 TPM
MAX_IN_FLIGHT = 6              # Safe concurrency ceiling: ramp showed 4 ok, 8 hit 429
DEFAULT_RETRY_BUDGET = 2       # Per-request retry budget
PRESSURE_QUEUE_THRESHOLD = 4   # Queue length triggering graceful degradation
PRESSURE_RPM_THRESHOLD = 0.80  # 80% of RPM consumed triggers degradation
PRESSURE_TPM_THRESHOLD = 0.80  # 80% of TPM consumed triggers degradation
DEFAULT_COOLDOWN_SECONDS = 2.0 # Default cooldown on 429 when retry-after header missing

# Price Configuration for Table 2b cost calculation ($ per 1,000,000 tokens)
# Source: Groq pricing documentation for developer tier on-demand models
INPUT_TOKEN_PRICE_PER_M = 0.05    # $0.05 per 1M input / prompt tokens
OUTPUT_TOKEN_PRICE_PER_M = 0.08   # $0.08 per 1M output / completion tokens
