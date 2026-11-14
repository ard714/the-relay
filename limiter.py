"""
limiter.py - Shared rate limiter, concurrency controller, and resilient retry policy.

Policy requirements:
- Shared limiter: in-flight cap, sliding-window RPM and TPM budget (from config.py)
- On 429: read retry-after. If retry-after > time left (deadline), do NOT wait (shed or degrade)
- One 429 sets a shared cooldown for everybody to prevent stampedes
- Retry only the step that failed, never restart the whole pipeline
- Backoff with full jitter for 5xx and timeouts; no retry for other 4xx; per-request retry budget
- Daily-limit 429 detected separately and flips flag: no more LLM calls until reset
- Under pressure (queue long or minute budget nearly used) degrade in stages:
    Stage 1: skip understand step
    Stage 2: fewer chunks retrieved
    Stage 3: skip LLM verify (rely on free code check)
"""

import asyncio
from collections import deque
import json
import random
import re
import sys
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from config import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_RETRY_BUDGET,
    LIMIT_RPM,
    LIMIT_TPM,
    MAX_IN_FLIGHT,
    PRESSURE_QUEUE_THRESHOLD,
    PRESSURE_RPM_THRESHOLD,
    PRESSURE_TPM_THRESHOLD,
)


class SharedLimiter:
    """
    Central shared limiter governing all outbound model calls.
    Maintains in-flight semaphore, sliding-window RPM/TPM accounting,
    shared 429 cooldown, and daily budget exhaustion state.
    """

    def __init__(
        self,
        rpm_limit: int = LIMIT_RPM,
        tpm_limit: int = LIMIT_TPM,
        max_in_flight: int = MAX_IN_FLIGHT,
        queue_threshold: int = PRESSURE_QUEUE_THRESHOLD,
    ):
        self.rpm_limit = rpm_limit
        self.tpm_limit = tpm_limit
        self.max_in_flight = max_in_flight
        self.queue_threshold = queue_threshold

        self.semaphore = asyncio.Semaphore(max_in_flight)
        self.sliding_window: deque[Tuple[float, int]] = deque()  # (timestamp, tokens)
        self.lock = asyncio.Lock()

        # Shared Cooldown & Daily Limit State
        self.shared_cooldown_until: float = 0.0
        self.daily_budget_exhausted: bool = False
        self.queue_waiters: int = 0

    def is_daily_exhausted(self) -> bool:
        return self.daily_budget_exhausted

    def reset(self) -> None:
        """Reset sliding window, in-flight counters, cooldowns, and daily flag."""
        self.semaphore = asyncio.Semaphore(self.max_in_flight)
        self.sliding_window.clear()
        self.shared_cooldown_until = 0.0
        self.daily_budget_exhausted = False
        self.queue_waiters = 0

    def set_daily_exhausted(self, state: bool = True) -> None:
        self.daily_budget_exhausted = state

    def set_shared_cooldown(self, cooldown_seconds: float) -> None:
        """Set shared cooldown timestamp so other requests do not stampede."""
        now = time.monotonic()
        target = now + max(0.5, cooldown_seconds)
        if target > self.shared_cooldown_until:
            self.shared_cooldown_until = target

    def get_remaining_cooldown(self) -> float:
        now = time.monotonic()
        return max(0.0, self.shared_cooldown_until - now)

    def _clean_window(self, now: float) -> None:
        cutoff = now - 60.0
        while self.sliding_window and self.sliding_window[0][0] < cutoff:
            self.sliding_window.popleft()

    def get_current_rpm(self) -> int:
        now = time.time()
        self._clean_window(now)
        return len(self.sliding_window)

    def get_current_tpm(self) -> int:
        now = time.time()
        self._clean_window(now)
        return sum(item[1] for item in self.sliding_window)

    def get_pressure_stage(self) -> int:
        """
        Determines graceful degradation stage (0 to 3):
        - Stage 0: Normal operation
        - Stage 1: Skip query understand step (high queue or RPM > 80%)
        - Stage 2: Fewer retrieved chunks (TPM > 80% or high queue)
        - Stage 3: Skip LLM verify check (near limits: RPM/TPM > 90% or heavy queue)
        """
        if self.daily_budget_exhausted:
            return 3

        now = time.time()
        self._clean_window(now)

        current_rpm = len(self.sliding_window)
        current_tpm = sum(item[1] for item in self.sliding_window)

        rpm_ratio = current_rpm / max(1, self.rpm_limit)
        tpm_ratio = current_tpm / max(1, self.tpm_limit)
        q_len = self.queue_waiters

        if rpm_ratio >= 0.90 or tpm_ratio >= 0.90 or q_len >= self.queue_threshold * 2:
            return 3
        if rpm_ratio >= PRESSURE_RPM_THRESHOLD or tpm_ratio >= PRESSURE_TPM_THRESHOLD or q_len >= self.queue_threshold:
            return 2
        if rpm_ratio >= 0.65 or q_len >= 2:
            return 1

        return 0

    async def acquire_slot(
        self,
        estimated_tokens: int = 150,
        deadline_remaining: float = 30.0,
    ) -> Tuple[bool, str, float]:
        """
        Acquire rate-limiter slot under in-flight cap, sliding-window budget, and cooldown.
        Returns: (acquired: bool, reason: str, wait_time_ms: float)
        """
        if self.daily_budget_exhausted:
            return False, "Daily limit exhausted. No more LLM calls allowed.", 0.0

        t_start = time.perf_counter()
        self.queue_waiters += 1

        try:
            # 1. Respect shared 429 cooldown first
            cd_wait = self.get_remaining_cooldown()
            if cd_wait > 0:
                if cd_wait > deadline_remaining:
                    return False, f"Shared cooldown ({cd_wait:.2f}s) exceeds user deadline ({deadline_remaining:.2f}s). Shedding request.", 0.0
                await asyncio.sleep(cd_wait)

            # 2. In-flight concurrency semaphore
            sem_acquired = False
            time_left = max(0.1, deadline_remaining - (time.perf_counter() - t_start))
            try:
                await asyncio.wait_for(self.semaphore.acquire(), timeout=time_left)
                sem_acquired = True
            except asyncio.TimeoutError:
                return False, f"In-flight semaphore acquisition timed out ({time_left:.2f}s). Shedding request.", (time.perf_counter() - t_start) * 1000

            # 3. Sliding window budget check
            async with self.lock:
                now = time.time()
                self._clean_window(now)

                # If RPM or TPM budget would be exceeded, calculate wait
                current_rpm = len(self.sliding_window)
                current_tpm = sum(item[1] for item in self.sliding_window)

                if (current_rpm >= self.rpm_limit) or (current_tpm + estimated_tokens > self.tpm_limit):
                    if self.sliding_window:
                        oldest_ts = self.sliding_window[0][0]
                        needed_sleep = max(0.1, (oldest_ts + 60.0) - now)
                        cur_left = max(0.1, deadline_remaining - (time.perf_counter() - t_start))
                        if needed_sleep > cur_left:
                            if sem_acquired:
                                self.semaphore.release()
                            return False, f"Rate budget wait ({needed_sleep:.2f}s) exceeds deadline ({cur_left:.2f}s). Shedding request.", (time.perf_counter() - t_start) * 1000
                        await asyncio.sleep(needed_sleep)
                        now = time.time()
                        self._clean_window(now)

                # Register request in sliding window
                self.sliding_window.append((now, estimated_tokens))

            t_end = time.perf_counter()
            wait_ms = round((t_end - t_start) * 1000, 2)
            return True, "Slot acquired", wait_ms

        finally:
            self.queue_waiters = max(0, self.queue_waiters - 1)

    def release_slot(self) -> None:
        """Release concurrency semaphore."""
        try:
            self.semaphore.release()
        except ValueError:
            pass


# Global singleton instance
GLOBAL_LIMITER = SharedLimiter()


def parse_retry_after(headers: Dict[str, str], error_body: Optional[str] = None) -> float:
    """Extract retry delay from headers or error message body."""
    # Check headers
    for k in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        if k in headers and headers[k]:
            val = str(headers[k]).strip()
            # plain number
            try:
                return max(0.5, float(val))
            except ValueError:
                pass
            # minute/second format: e.g. 2m52.8s
            total = 0.0
            m = re.search(r"(\d+(?:\.\d+)?)m", val)
            if m:
                total += float(m.group(1)) * 60.0
            s = re.search(r"(\d+(?:\.\d+)?)s", val)
            if s and not re.search(r"\d+ms", val):
                total += float(s.group(1))
            ms = re.search(r"(\d+(?:\.\d+)?)ms", val)
            if ms:
                total += float(ms.group(1)) / 1000.0
            if total > 0:
                return max(0.5, total)

    # Check error body text: "Please try again in 2s."
    if error_body:
        match = re.search(r"try again in (\d+(?:\.\d+)?)(m?s)", error_body)
        if match:
            v = float(match.group(1))
            unit = match.group(2)
            return max(0.5, v / 1000.0 if unit == "ms" else v)

    return DEFAULT_COOLDOWN_SECONDS


def is_daily_quota_error(status_code: int, error_body: Optional[str], headers: Dict[str, str]) -> bool:
    """Detect if a 429 response indicates daily quota exhaustion."""
    if status_code != 429:
        return False
    body = (error_body or "").lower()
    if (
        "requests per day" in body
        or "tokens per day" in body
        or "rpd" in body
        or "tpd" in body
        or "daily quota" in body
        or "daily limit" in body
    ):
        return True
    return False


async def execute_step_with_retry(
    step_name: str,
    coro_fn: Callable[[], Coroutine[Any, Any, Dict[str, Any]]],
    deadline_remaining_fn: Callable[[], float],
    limiter: SharedLimiter = GLOBAL_LIMITER,
    retry_budget: int = DEFAULT_RETRY_BUDGET,
    estimated_tokens: int = 150,
) -> Tuple[Dict[str, Any], float, float, int]:
    """
    Executes a single pipeline step with isolated retries (NEVER restarts whole pipeline).
    Handles:
    - In-flight acquisition and budget check
    - Shared cooldown enforcement
    - 429 retry-after vs deadline comparison (sheds if retry-after > deadline)
    - Exponential backoff with full jitter for 5xx and timeouts
    - No retry for other 4xx (400, 401, 403, 404)
    - Daily quota detection flips shared flag

    Returns: (result_dict, work_ms, wait_ms, retry_ms, retries_used)
    """
    total_wait_ms = 0.0
    total_retry_ms = 0.0
    retries_used = 0
    base_backoff = 0.5

    for attempt in range(retry_budget + 1):
        rem_dl = deadline_remaining_fn()
        if rem_dl <= 0.2:
            return (
                {"status": "timeout", "status_code": 408, "error_body": "User deadline exceeded before step dispatch.", "content": ""},
                0.0,
                total_wait_ms,
                total_retry_ms,
                retries_used,
            )

        # Acquire slot from shared limiter
        slot_ok, slot_reason, wait_ms = await limiter.acquire_slot(
            estimated_tokens=estimated_tokens,
            deadline_remaining=rem_dl,
        )
        total_wait_ms += wait_ms
        if not slot_ok:
            # Dropped or shed
            return (
                {"status": "rate_limited", "status_code": 429, "error_body": slot_reason, "content": ""},
                0.0,
                total_wait_ms,
                total_retry_ms,
                retries_used,
            )

        t_work_start = time.perf_counter()
        try:
            res = await coro_fn()
        finally:
            limiter.release_slot()
        t_work_end = time.perf_counter()
        work_ms = round((t_work_end - t_work_start) * 1000, 2)

        status_code = res.get("status_code", 200)
        status = res.get("status", "ok")

        # Success case
        if status == "ok" and (status_code is None or status_code == 200):
            return res, work_ms, total_wait_ms, total_retry_ms, retries_used

        # Check for daily limit exhaustion
        if is_daily_quota_error(status_code, res.get("error_body"), res.get("rate_limit_headers", {})):
            limiter.set_daily_exhausted(True)
            return res, work_ms, total_wait_ms, total_retry_ms, retries_used

        # 429 Rate Limited
        if status_code == 429 or status == "rate_limited":
            headers = res.get("rate_limit_headers", {})
            error_body = res.get("error_body")
            retry_after = parse_retry_after(headers, error_body)

            # Broadcast shared cooldown so all concurrent workers avoid stampeding
            limiter.set_shared_cooldown(retry_after)

            cur_time_left = deadline_remaining_fn()
            # KEY REQUIREMENT: If retry-after is longer than user's remaining deadline, do NOT wait!
            if retry_after > cur_time_left:
                res["error_body"] = (
                    f"Rate limited (429). Retry-after ({retry_after:.2f}s) exceeds remaining user deadline "
                    f"({cur_time_left:.2f}s). Shedding request without retrying."
                )
                return res, work_ms, total_wait_ms, total_retry_ms, retries_used

            if attempt < retry_budget:
                retries_used += 1
                t_ret_start = time.perf_counter()
                await asyncio.sleep(retry_after)
                t_ret_end = time.perf_counter()
                total_retry_ms += (t_ret_end - t_ret_start) * 1000
                continue
            else:
                return res, work_ms, total_wait_ms, total_retry_ms, retries_used

        # 5xx Server Errors or Timeouts -> Exponential Backoff with FULL JITTER
        if (status_code and status_code >= 500) or status == "timeout":
            if attempt < retry_budget:
                retries_used += 1
                t_ret_start = time.perf_counter()
                jittered_backoff = random.uniform(0.1, base_backoff * (2 ** attempt))
                cur_left = deadline_remaining_fn()
                if jittered_backoff > cur_left:
                    res["error_body"] = "Backoff sleep exceeds user deadline. Shedding request."
                    return res, work_ms, total_wait_ms, total_retry_ms, retries_used
                await asyncio.sleep(jittered_backoff)
                t_ret_end = time.perf_counter()
                total_retry_ms += (t_ret_end - t_ret_start) * 1000
                continue
            else:
                return res, work_ms, total_wait_ms, total_retry_ms, retries_used

        # Other 4xx client errors (400, 401, 403, 404) -> Do NOT retry
        return res, work_ms, total_wait_ms, total_retry_ms, retries_used

    return res, work_ms, total_wait_ms, total_retry_ms, retries_used

