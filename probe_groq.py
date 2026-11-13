"""
probe_groq.py - Probe Groq API to measure baseline latency, TTFT, and rate limit headers.

Usage:
    python probe_groq.py [--count N] [--model MODEL]
"""

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional
from dotenv import load_dotenv
import httpx

load_dotenv()

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b"


def get_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        print("ERROR: GROQ_API_KEY environment variable is not set.", file=sys.stderr)
        print("Set it via export GROQ_API_KEY='...' or put it in .env", file=sys.stderr)
        sys.exit(1)
    return key


async def send_single_probe(
    client: httpx.AsyncClient,
    api_key: str,
    prompt: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 150,
) -> Dict[str, Any]:
    """Send a single streaming chat completion request and measure TTFT and total latency."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    start_time = time.perf_counter()
    first_token_time: Optional[float] = None
    chunks_received = 0
    full_content = []
    usage_info: Dict[str, Any] = {}
    rate_limit_headers: Dict[str, str] = {}
    status_code: Optional[int] = None
    error_message: Optional[str] = None

    try:
        async with client.stream(
            "POST",
            GROQ_API_URL,
            headers=headers,
            json=payload,
            timeout=30.0,
        ) as response:
            status_code = response.status_code

            # Capture rate-limit headers
            for k, v in response.headers.items():
                if k.lower().startswith("x-ratelimit-") or k.lower() == "retry-after":
                    rate_limit_headers[k.lower()] = v

            if response.status_code != 200:
                error_body = await response.aread()
                error_message = error_body.decode(errors="replace")
                end_time = time.perf_counter()
                return {
                    "status": "error" if response.status_code != 429 else "rate_limited",
                    "status_code": status_code,
                    "first_token_ms": None,
                    "total_ms": (end_time - start_time) * 1000,
                    "error_body": error_message,
                    "rate_limit_headers": rate_limit_headers,
                }

            async for line in response.aiter_lines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        # Check usage if present
                        if "usage" in data and data["usage"]:
                            usage_info = data["usage"]
                        choices = data.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                if first_token_time is None:
                                    first_token_time = time.perf_counter()
                                chunks_received += 1
                                full_content.append(content)
                    except json.JSONDecodeError:
                        pass

        end_time = time.perf_counter()

        ttft_ms = (
            (first_token_time - start_time) * 1000
            if first_token_time is not None
            else None
        )
        total_ms = (end_time - start_time) * 1000

        return {
            "status": "ok",
            "status_code": status_code,
            "first_token_ms": round(ttft_ms, 2) if ttft_ms else None,
            "total_ms": round(total_ms, 2),
            "chunks_count": chunks_received,
            "content_preview": "".join(full_content)[:80],
            "usage": usage_info,
            "rate_limit_headers": rate_limit_headers,
        }

    except Exception as exc:
        end_time = time.perf_counter()
        return {
            "status": "exception",
            "status_code": status_code,
            "first_token_ms": None,
            "total_ms": round((end_time - start_time) * 1000, 2),
            "error_body": str(exc),
            "rate_limit_headers": rate_limit_headers,
        }


async def run_probe(count: int = 5, model: str = DEFAULT_MODEL) -> None:
    api_key = get_api_key()
    print(f"=== Probing Groq API ({model}) with {count} sequential requests ===")

    test_prompts = [
        "Explain what multi-head attention does in two short sentences.",
        "What is the key idea behind the Moshi real-time dialogue model?",
        "Why is dynamic chunking useful for hierarchical sequence modeling?",
        "State the time complexity of scaled dot-product attention in terms of sequence length n and dimension d.",
        "What are the benefits of streaming speech tokens compared to standard text models?",
    ]

    results: List[Dict[str, Any]] = []

    async with httpx.AsyncClient() as client:
        for i in range(count):
            prompt = test_prompts[i % len(test_prompts)]
            print(f"\n[Request {i+1}/{count}] Sending: '{prompt[:45]}...'")
            result = await send_single_probe(client, api_key, prompt, model=model)
            results.append(result)

            if result["status"] == "ok":
                print(
                    f"  Status: OK | TTFT: {result['first_token_ms']} ms | Total: {result['total_ms']} ms"
                )
                print(f"  Preview: {result['content_preview']}...")
            else:
                print(
                    f"  Status: {result['status']} (code {result['status_code']}) | Total: {result['total_ms']} ms"
                )
                print(f"  Error: {result.get('error_body')}")

            # Brief pause between sequential probes
            if i < count - 1:
                await asyncio.sleep(0.5)

    # Extract headers from the latest successful response
    latest_headers = {}
    for r in reversed(results):
        if r.get("rate_limit_headers"):
            latest_headers = r["rate_limit_headers"]
            break

    print("\n" + "=" * 50)
    print("=== RATE LIMIT HEADERS DISCOVERED ===")
    for k, v in sorted(latest_headers.items()):
        print(f"  {k}: {v}")

    # Latency summary
    valid_ttft = [r["first_token_ms"] for r in results if r.get("first_token_ms")]
    valid_total = [r["total_ms"] for r in results if r["status"] == "ok"]

    if valid_total:
        import statistics

        print("\n=== LATENCY SUMMARY (Real Groq Measurements) ===")
        print(f"  Requests sent: {len(results)}")
        print(f"  Successful: {len(valid_total)}")
        print(f"  TTFT (median): {statistics.median(valid_ttft):.1f} ms")
        print(f"  TTFT (min/max): {min(valid_ttft):.1f} / {max(valid_ttft):.1f} ms")
        print(f"  Total time (median): {statistics.median(valid_total):.1f} ms")
        print(f"  Total time (min/max): {min(valid_total):.1f} / {max(valid_total):.1f} ms")

    # Save to json file for calibrating the stand-in
    output_path = "groq_baseline.json"
    data = {
        "model": model,
        "timestamp": time.time(),
        "rate_limit_headers": latest_headers,
        "runs": results,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"\nRaw probe data saved to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Probe Groq API baseline.")
    parser.add_argument("--count", type=int, default=5, help="Number of probe requests (default: 5)")
    parser.add_argument("--model", type=str, default=DEFAULT_MODEL, help=f"Groq model (default: {DEFAULT_MODEL})")
    args = parser.parse_args()

    asyncio.run(run_probe(count=args.count, model=args.model))


if __name__ == "__main__":
    main()

