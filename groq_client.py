"""
groq_client.py - Async streaming client for Groq API with zero retries.

Logs every call as a JSON line to data/raw_real.jsonl with:
- time.perf_counter() timestamps (queue entry, dispatch, first content delta, stream end)
- HTTP status code and status string (ok, rate_limited, timeout, error)
- Streamed tokens in/out
- All x-ratelimit-* and retry-after headers (on 200 and 429)
- Raw 429 body text
"""

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from dotenv import load_dotenv
import httpx

from config import GROQ_API_URL, GROQ_MODEL

load_dotenv()

DATA_DIR = Path("data")
RAW_LOG_PATH = DATA_DIR / "raw_real.jsonl"


def get_api_key() -> str:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        print("ERROR: GROQ_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)
    return key


class GroqClient:
    """Async streaming client for Groq with instrumentation and zero retries."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = GROQ_MODEL,
        log_path: Path = RAW_LOG_PATH,
    ):
        self.api_key = api_key or get_api_key()
        self.model = model
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._client: Optional[httpx.AsyncClient] = None

    async def get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=45.0)
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self):
        await self.get_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 150,
        temperature: float = 0.7,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a streaming request to Groq API.
        Does NOT retry on error.
        Logs every request to data/raw_real.jsonl.
        """
        req_id = request_id or f"req_{uuid.uuid4().hex[:12]}"
        target_model = model or self.model
        t_queue_entry = time.perf_counter()

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": target_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        client = await self.get_client()
        t_dispatch = time.perf_counter()
        t_first_token: Optional[float] = None
        t_first_chunk: Optional[float] = None
        chunks_text: List[str] = []
        tokens_in = 0
        tokens_out = 0
        status_code: Optional[int] = None
        rate_limit_headers: Dict[str, str] = {}
        retry_after: Optional[str] = None
        error_body: Optional[str] = None
        status = "error"

        try:
            async with client.stream(
                "POST",
                GROQ_API_URL,
                headers=headers,
                json=payload,
            ) as response:
                status_code = response.status_code

                # Extract all rate limit and retry headers
                for k, v in response.headers.items():
                    k_lower = k.lower()
                    if k_lower.startswith("x-ratelimit-") or k_lower == "retry-after":
                        rate_limit_headers[k_lower] = v
                        if k_lower == "retry-after":
                            retry_after = v

                if status_code == 200:
                    status = "ok"
                    async for line in response.aiter_lines():
                        line = line.strip()
                        if not line or not line.startswith("data: "):
                            continue
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            # Check usage
                            if "usage" in data and data["usage"]:
                                usage = data["usage"]
                                tokens_in = usage.get("prompt_tokens", tokens_in)
                                tokens_out = usage.get("completion_tokens", tokens_out)
                            choices = data.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                reasoning = delta.get("reasoning", "")
                                # First non-empty content delta as required by brief
                                if content:
                                    if t_first_token is None:
                                        t_first_token = time.perf_counter()
                                    chunks_text.append(content)
                                elif reasoning and t_first_token is None:
                                    # Track first token arrival even if model starts with reasoning
                                    if t_first_chunk is None:
                                        t_first_chunk = time.perf_counter()
                        except json.JSONDecodeError:
                            pass

                    # If usage chunk wasn't present, estimate tokens_out by words
                    if tokens_out == 0 and chunks_text:
                        full_txt = "".join(chunks_text)
                        tokens_out = max(1, len(full_txt.split()))
                    if tokens_in == 0:
                        prompt_words = sum(len(m.get("content", "").split()) for m in messages)
                        tokens_in = max(1, int(prompt_words * 1.3))

                elif status_code == 429:
                    status = "rate_limited"
                    body_bytes = await response.aread()
                    error_body = body_bytes.decode(errors="replace")
                else:
                    status = "error"
                    body_bytes = await response.aread()
                    error_body = body_bytes.decode(errors="replace")

        except httpx.TimeoutException as exc:
            status = "timeout"
            error_body = f"TimeoutException: {str(exc)}"
        except Exception as exc:
            status = "error"
            error_body = f"Exception: {str(exc)}"

        t_stream_end = time.perf_counter()

        # Fallback to first token/chunk arrival if content delta was purely reasoning
        effective_first_token = t_first_token if t_first_token is not None else t_first_chunk
        ttft_ms = (
            round((effective_first_token - t_dispatch) * 1000, 2)
            if effective_first_token is not None
            else None
        )
        total_ms = round((t_stream_end - t_dispatch) * 1000, 2)
        queue_delay_ms = round((t_dispatch - t_queue_entry) * 1000, 2)

        # Log entry
        log_entry = {
            "request_id": req_id,
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "model": target_model,
            "queue_entry_time": t_queue_entry,
            "dispatch_time": t_dispatch,
            "first_token_time": t_first_token,
            "stream_end_time": t_stream_end,
            "queue_delay_ms": queue_delay_ms,
            "ttft_ms": ttft_ms,
            "total_ms": total_ms,
            "status": status,
            "status_code": status_code,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "rate_limit_headers": rate_limit_headers,
            "retry_after": retry_after,
            "error_body": error_body,
        }

        # Append to raw_real.jsonl
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry) + "\n")
        except Exception as log_err:
            print(f"Warning: Failed to write to {self.log_path}: {log_err}", file=sys.stderr)

        return {
            "request_id": req_id,
            "status": status,
            "status_code": status_code,
            "ttft_ms": ttft_ms,
            "total_ms": total_ms,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "rate_limit_headers": rate_limit_headers,
            "retry_after": retry_after,
            "content": "".join(chunks_text),
            "error_body": error_body,
        }

