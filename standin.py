"""
standin.py - Controllable stand-in model service for load testing.

Mimics /v1/chat/completions with SSE streaming, calibrated from real Groq measurements.
Endpoints:
  POST /v1/chat/completions - Streamed or non-streamed chat completions
  POST /admin/config        - Update knobs live
  GET  /admin/config        - Read active configuration
  POST /admin/reset         - Reset counters, sliding windows, and in-flight tracking
  POST /admin/exhaust_day   - Manually flip daily budget exhaustion flag (3pm scenario)
  GET  /admin/stats         - Current metrics and live queue state
"""

import argparse
import asyncio
from collections import deque
import json
import math
import random
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import web

# Placeholders marked CALIBRATE until filled from empirical measurements
STANDIN_DEFAULT_CONFIG: Dict[str, Any] = {
    # 1. Latency Knobs (ms)
    "ttft_p50_ms": None,                # CALIBRATE: median time to first token
    "ttft_p95_ms": None,                # CALIBRATE: 95th percentile TTFT
    "per_token_ms": None,               # CALIBRATE: generation latency per token
    "slowdown_per_in_flight_ms": None,  # CALIBRATE: extra delay per concurrent request
    # 2. Failure & Anomaly Rates
    "random_5xx_rate": 0.0,             # CALIBRATE: probability of 500/503 error
    "rare_slow_rate": 0.0,              # CALIBRATE: probability of anomalously slow request
    "rare_slow_multiplier": 3.5,        # CALIBRATE: latency multiplier for rare slow success
    # 3. Rate & Quota Limits
    "in_flight_cap": None,              # CALIBRATE: max concurrent requests
    "rpm_limit": None,                  # CALIBRATE: requests per minute
    "tpm_limit": None,                  # CALIBRATE: tokens per minute
    "rpd_limit": None,                  # CALIBRATE: requests per day
    "tpd_limit": None,                  # CALIBRATE: tokens per day
    "retry_after_seconds": None,        # CALIBRATE: retry-after header value
    # 4. Identity & Error Shaping
    "model_name": "openai/gpt-oss-20b",
    "org_id": "org_01kscc7p5jf1evbn7xdybac1dy",
}

# Canned realistic responses for papers
CANNED_RESPONSES = [
    (
        "Moshi models real-time spoken dialogue by decomposing audio into discrete semantic "
        "and acoustic tokens using the Mimi codec [Moshi, §3.3, p. 9]. It predicts user and agent "
        "streams simultaneously to sustain duplex interaction [Moshi, §1, p. 2]."
    ),
    (
        "Scaled dot-product attention computes queries and keys with dimension d_k, scaling "
        "the inner product by 1/sqrt(d_k) to prevent vanishing gradients in the softmax [Attention, §3.2.1, p. 4]. "
        "Multi-head attention projects queries, keys, and values across h distinct representation subspaces [Attention, §3.2.2, p. 5]."
    ),
    (
        "Dynamic Chunking (H-Net) adaptively groups sequences into variable-length chunks "
        "using router decision boundaries [DynChunk, §2.2, p. 6]. This achieves hierarchical "
        "compression while maintaining autoregressive causality [DynChunk, §2.4, p. 9]."
    ),
    "The papers do not say.",
]


class StandinState:
    """Maintains active configuration, sliding-window rate tracking, and in-flight requests."""

    def __init__(self, initial_config: Optional[Dict[str, Any]] = None):
        self.config: Dict[str, Any] = dict(STANDIN_DEFAULT_CONFIG)
        if initial_config:
            self.config.update(initial_config)

        self.in_flight_count: int = 0
        self.daily_exhausted: bool = False
        self.total_served: int = 0
        self.total_failed_5xx: int = 0
        self.total_rate_limited_429: int = 0

        # Sliding window for 60-second RPM/TPM: deque of (monotonic_timestamp, token_count)
        self.sliding_window: deque[Tuple[float, int]] = deque()
        # Daily usage counters
        self.daily_requests_used: int = 0
        self.daily_tokens_used: int = 0

        self.lock = asyncio.Lock()

    def clean_sliding_window(self, now: float) -> None:
        cutoff = now - 60.0
        while self.sliding_window and self.sliding_window[0][0] < cutoff:
            self.sliding_window.popleft()

    def get_current_rpm(self, now: float) -> int:
        self.clean_sliding_window(now)
        return len(self.sliding_window)

    def get_current_tpm(self, now: float) -> int:
        self.clean_sliding_window(now)
        return sum(item[1] for item in self.sliding_window)

    def reset(self) -> None:
        self.in_flight_count = 0
        self.daily_exhausted = False
        self.sliding_window.clear()
        self.daily_requests_used = 0
        self.daily_tokens_used = 0
        self.total_served = 0
        self.total_failed_5xx = 0
        self.total_rate_limited_429 = 0


def format_groq_429_body(
    model: str,
    org: str,
    limit_type: str,
    limit: int,
    used: int,
    requested: int,
    retry_after: float,
) -> str:
    """Produce the exact error payload returned by Groq."""
    return json.dumps({
        "error": {
            "message": (
                f"Rate limit reached for model `{model}` in organization `{org}` service tier `on_demand` "
                f"on {limit_type}: Limit {limit}, Used {used}, Requested {requested}. "
                f"Please try again in {retry_after:g}s. Need more tokens? Upgrade to Dev Tier today at https://console.groq.com/settings/billing"
            ),
            "type": "requests" if "RPM" in limit_type or "RPD" in limit_type else "tokens",
            "code": "rate_limit_exceeded",
        }
    })


def sample_ttft_latency(cfg: Dict[str, Any], in_flight: int) -> float:
    """Sample time-to-first-token based on calibrated p50/p95 and in-flight load."""
    p50 = cfg.get("ttft_p50_ms") or 500.0
    p95 = cfg.get("ttft_p95_ms") or 1500.0
    slowdown = (cfg.get("slowdown_per_in_flight_ms") or 30.0) * in_flight

    # Fit a log-normal distribution from p50 and p95
    mu = math.log(max(10.0, p50))
    sigma = max(0.1, (math.log(max(11.0, p95)) - mu) / 1.645)

    base_ms = random.lognormvariate(mu, sigma)
    total_ms = base_ms + slowdown

    # Check for rare very slow success
    rare_rate = cfg.get("rare_slow_rate") or 0.0
    if random.random() < rare_rate:
        mult = cfg.get("rare_slow_multiplier") or 3.5
        total_ms *= mult

    return max(20.0, total_ms) / 1000.0  # seconds


async def handle_chat_completions(request: web.Request) -> web.Response:
    state: StandinState = request.app["state"]
    cfg = state.config

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": {"message": "Invalid JSON payload"}}, status=400)

    model = body.get("model", cfg.get("model_name", "openai/gpt-oss-20b"))
    messages = body.get("messages", [])
    max_tokens = body.get("max_tokens", 150)
    stream = body.get("stream", False)

    now = time.monotonic()
    now_wall = time.time()

    # 1. Check Daily Limit Exhaustion
    if state.daily_exhausted or (cfg.get("rpd_limit") and state.daily_requests_used >= cfg["rpd_limit"]):
        state.daily_exhausted = True
        state.total_rate_limited_429 += 1
        rpd = cfg.get("rpd_limit") or 14400
        error_json = format_groq_429_body(
            model=model,
            org=cfg["org_id"],
            limit_type="requests per day (RPD)",
            limit=rpd,
            used=rpd,
            requested=1,
            retry_after=43200.0,
        )
        return web.Response(
            status=429,
            body=error_json,
            content_type="application/json",
            headers={"retry-after": "43200", "x-ratelimit-reset-requests": "12h0m0s"},
        )

    # 2. Check In-Flight Concurrency Cap
    in_flight_cap = cfg.get("in_flight_cap")
    if in_flight_cap and state.in_flight_count >= in_flight_cap:
        state.total_rate_limited_429 += 1
        error_json = format_groq_429_body(
            model=model,
            org=cfg["org_id"],
            limit_type="concurrent requests in flight",
            limit=in_flight_cap,
            used=state.in_flight_count,
            requested=1,
            retry_after=cfg.get("retry_after_seconds") or 2.0,
        )
        return web.Response(
            status=429,
            body=error_json,
            content_type="application/json",
            headers={"retry-after": str(int(cfg.get("retry_after_seconds") or 2))},
        )

    # 3. Check Sliding Window RPM & TPM Limits
    async with state.lock:
        state.clean_sliding_window(now)
        current_rpm = len(state.sliding_window)
        current_tpm = sum(item[1] for item in state.sliding_window)

        rpm_limit = cfg.get("rpm_limit")
        tpm_limit = cfg.get("tpm_limit")
        est_tokens = max_tokens

        if (rpm_limit and current_rpm >= rpm_limit) or (tpm_limit and current_tpm + est_tokens > tpm_limit):
            state.total_rate_limited_429 += 1
            retry_after = cfg.get("retry_after_seconds") or 2.0
            limit_str = "requests per minute (RPM)" if (rpm_limit and current_rpm >= rpm_limit) else "tokens per minute (TPM)"
            lim_val = rpm_limit if "RPM" in limit_str else tpm_limit
            used_val = current_rpm if "RPM" in limit_str else current_tpm
            error_json = format_groq_429_body(
                model=model,
                org=cfg["org_id"],
                limit_type=limit_str,
                limit=lim_val,
                used=used_val,
                requested=1 if "RPM" in limit_str else est_tokens,
                retry_after=retry_after,
            )
            return web.Response(
                status=429,
                body=error_json,
                content_type="application/json",
                headers={
                    "retry-after": str(int(retry_after)),
                    "x-ratelimit-limit-requests": str(rpm_limit or 1000),
                    "x-ratelimit-remaining-requests": "0",
                    "x-ratelimit-reset-requests": f"{retry_after:.1f}s",
                },
            )

        # Record admission into sliding window
        state.sliding_window.append((now, est_tokens))
        state.daily_requests_used += 1
        state.daily_tokens_used += est_tokens
        state.in_flight_count += 1

    try:
        # 4. Check Random 5xx Fault Injection
        random_5xx = cfg.get("random_5xx_rate") or 0.0
        if random.random() < random_5xx:
            state.total_failed_5xx += 1
            await asyncio.sleep(0.08)
            return web.json_response(
                {"error": {"message": "Internal server error", "type": "server_error", "code": 500}},
                status=500,
            )

        # 5. Determine Response Text
        user_text = "".join(m.get("content", "") for m in messages if m.get("role") == "user")
        if "moshi" in user_text.lower():
            response_text = CANNED_RESPONSES[0]
        elif "attention" in user_text.lower() or "dot" in user_text.lower():
            response_text = CANNED_RESPONSES[1]
        elif "chunk" in user_text.lower():
            response_text = CANNED_RESPONSES[2]
        else:
            response_text = random.choice(CANNED_RESPONSES)

        # 6. Simulate Latency
        ttft_delay = sample_ttft_latency(cfg, state.in_flight_count)
        per_token_delay = (cfg.get("per_token_ms") or 8.0) / 1000.0

        rate_headers = {
            "x-ratelimit-limit-requests": str(cfg.get("rpm_limit") or 1000),
            "x-ratelimit-limit-tokens": str(cfg.get("tpm_limit") or 8000),
            "x-ratelimit-remaining-requests": str(max(0, (cfg.get("rpm_limit") or 1000) - current_rpm)),
            "x-ratelimit-remaining-tokens": str(max(0, (cfg.get("tpm_limit") or 8000) - current_tpm)),
            "x-ratelimit-reset-requests": "1m0s",
            "x-ratelimit-reset-tokens": "2.5s",
        }

        # 7. Non-Streaming Response
        if not stream:
            await asyncio.sleep(ttft_delay + (len(response_text.split()) * per_token_delay))
            state.total_served += 1
            return web.json_response(
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(now_wall),
                    "model": model,
                    "choices": [
                        {"index": 0, "message": {"role": "assistant", "content": response_text}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": len(user_text.split()),
                        "completion_tokens": len(response_text.split()),
                        "total_tokens": len(user_text.split()) + len(response_text.split()),
                    },
                },
                headers=rate_headers,
            )

        # 8. Streaming Response (SSE)
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                **rate_headers,
            },
        )
        await response.prepare(request)

        # Sleep until Time to First Token (TTFT)
        await asyncio.sleep(ttft_delay)

        # Stream words/tokens piece by piece
        tokens = response_text.split(" ")
        for i, word in enumerate(tokens):
            chunk_content = word if i == 0 else f" {word}"
            chunk = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(now_wall),
                "model": model,
                "choices": [{"index": 0, "delta": {"content": chunk_content}, "finish_reason": None}],
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
            if per_token_delay > 0:
                await asyncio.sleep(per_token_delay)

        # Stream usage & [DONE]
        usage_chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(now_wall),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": len(user_text.split()),
                "completion_tokens": len(tokens),
                "total_tokens": len(user_text.split()) + len(tokens),
            },
        }
        await response.write(f"data: {json.dumps(usage_chunk)}\n\n".encode("utf-8"))
        await response.write(b"data: [DONE]\n\n")

        state.total_served += 1
        return response

    finally:
        async with state.lock:
            state.in_flight_count = max(0, state.in_flight_count - 1)


# --- Admin Endpoints ---

async def handle_admin_config_get(request: web.Request) -> web.Response:
    state: StandinState = request.app["state"]
    return web.json_response(state.config)


async def handle_admin_config_post(request: web.Request) -> web.Response:
    state: StandinState = request.app["state"]
    try:
        new_cfg = await request.json()
        state.config.update(new_cfg)
        return web.json_response({"status": "updated", "config": state.config})
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)


async def handle_admin_reset(request: web.Request) -> web.Response:
    state: StandinState = request.app["state"]
    state.reset()
    return web.json_response({"status": "reset_successful"})


async def handle_admin_exhaust_day(request: web.Request) -> web.Response:
    state: StandinState = request.app["state"]
    state.daily_exhausted = True
    return web.json_response({"status": "daily_budget_exhausted", "daily_exhausted": True})


async def handle_admin_stats(request: web.Request) -> web.Response:
    state: StandinState = request.app["state"]
    now = time.monotonic()
    return web.json_response({
        "in_flight": state.in_flight_count,
        "current_rpm": state.get_current_rpm(now),
        "current_tpm": state.get_current_tpm(now),
        "daily_requests_used": state.daily_requests_used,
        "daily_tokens_used": state.daily_tokens_used,
        "daily_exhausted": state.daily_exhausted,
        "total_served": state.total_served,
        "total_429": state.total_rate_limited_429,
        "total_5xx": state.total_failed_5xx,
    })


def create_app(initial_config: Optional[Dict[str, Any]] = None) -> web.Application:
    app = web.Application()
    app["state"] = StandinState(initial_config=initial_config)

    app.router.add_post("/v1/chat/completions", handle_chat_completions)
    app.router.add_get("/admin/config", handle_admin_config_get)
    app.router.add_post("/admin/config", handle_admin_config_post)
    app.router.add_post("/admin/reset", handle_admin_reset)
    app.router.add_post("/admin/exhaust_day", handle_admin_exhaust_day)
    app.router.add_get("/admin/stats", handle_admin_stats)

    return app


def main():
    parser = argparse.ArgumentParser(description="Run stand-in model service.")
    parser.add_argument("--port", type=int, default=8000, help="Server port (default: 8000)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    args = parser.parse_args()

    app = create_app()
    print(f"Starting Stand-in Model Service on http://{args.host}:{args.port}")
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
