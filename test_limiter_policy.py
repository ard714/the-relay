"""
test_limiter_policy.py - Unit tests proving the rate limiter and retry policy invariants.

Proves:
1. No retry past the deadline: When retry-after > deadline remaining, does NOT wait.
2. No whole-pipeline restart: Retries only the isolated failed step, preserving prior step results.
3. Cooldown is shared: A 429 encountered by one worker sets a shared cooldown preventing stampedes.
"""

import asyncio
import time
import unittest
from typing import Any, Dict, List, Optional

from limiter import SharedLimiter, execute_step_with_retry
import pipeline


class ScriptedFakeClient:
    """Mock Groq client returning scripted HTTP responses and logging calls."""

    def __init__(self, responses: Optional[List[Dict[str, Any]]] = None):
        self.responses: List[Dict[str, Any]] = responses or []
        self.call_history: List[Dict[str, Any]] = []
        self.call_count = 0

    def add_response(self, resp: Dict[str, Any]) -> None:
        self.responses.append(resp)

    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 150,
        temperature: float = 0.7,
        model: Optional[str] = None,
        request_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.call_count += 1
        call_record = {
            "index": self.call_count,
            "timestamp": time.perf_counter(),
            "messages": messages,
            "timeout": timeout,
        }
        self.call_history.append(call_record)

        if self.responses:
            return self.responses.pop(0)

        # Default fallback
        return {
            "status": "ok",
            "status_code": 200,
            "content": "Default mock response [Attention, §3.2, p. 4].",
            "tokens_in": 50,
            "tokens_out": 20,
            "rate_limit_headers": {},
        }


class TestLimiterPolicy(unittest.IsolatedAsyncioTestCase):

    async def test_1_no_retry_past_deadline(self):
        """
        PROVE: If retry-after exceeds the remaining user deadline,
        the system does NOT sleep/wait and does NOT retry.
        It immediately sheds the request.
        """
        print("\n--- Running Test 1: No Retry Past Deadline ---")
        fake_client = ScriptedFakeClient([
            {
                "status": "rate_limited",
                "status_code": 429,
                "error_body": "Rate limit exceeded. Please try again in 5s.",
                "rate_limit_headers": {"retry-after": "5.0"},
                "content": "",
            }
        ])

        limiter = SharedLimiter()
        t_start = time.perf_counter()

        # User deadline is only 1.0 second, while retry-after is 5.0 seconds
        deadline_budget = 1.0

        def get_deadline_remaining():
            return max(0.0, deadline_budget - (time.perf_counter() - t_start))

        res, work_ms, wait_ms, retry_ms, retries_used = await execute_step_with_retry(
            step_name="test_step",
            coro_fn=lambda: fake_client.stream_chat([{"role": "user", "content": "hi"}]),
            deadline_remaining_fn=get_deadline_remaining,
            limiter=limiter,
            retry_budget=2,
        )

        t_end = time.perf_counter()
        elapsed_seconds = t_end - t_start

        # Assertions
        self.assertEqual(res["status_code"], 429)
        self.assertEqual(retries_used, 0, "Must not retry when retry-after > deadline")
        self.assertIn("exceeds remaining user deadline", res.get("error_body", ""))
        self.assertLess(
            elapsed_seconds,
            0.5,
            f"Must shed immediately rather than waiting. Elapsed: {elapsed_seconds:.3f}s",
        )
        self.assertEqual(fake_client.call_count, 1, "Must not have dispatched second call")
        print(f"  [PASS] Request shed in {elapsed_seconds*1000:.1f}ms without waiting 5.0s retry-after.")

    async def test_2_no_whole_pipeline_restart(self):
        """
        PROVE: When a later step fails and retries, only THAT step retries.
        The pipeline NEVER restarts from Step 1.
        """
        print("\n--- Running Test 2: No Whole-Pipeline Restart ---")
        understand_resp = {
            "status": "ok",
            "status_code": 200,
            "content": '{"is_off_topic": false, "search_query": "Moshi real-time"}',
            "tokens_in": 40,
            "tokens_out": 20,
            "rate_limit_headers": {},
        }
        # Draft step attempt 1: 429 with short retry-after (0.1s)
        draft_attempt_1 = {
            "status": "rate_limited",
            "status_code": 429,
            "error_body": "Rate limited. Please try again in 0.1s.",
            "rate_limit_headers": {"retry-after": "0.1"},
            "content": "",
        }
        # Draft step attempt 2: Succeeds with cited answer matching retrieved hit
        draft_attempt_2 = {
            "status": "ok",
            "status_code": 200,
            "content": "Moshi enables real-time spoken dialogue using discrete audio tokens. [Moshi, §2, p. 6]",
            "tokens_in": 120,
            "tokens_out": 30,
            "rate_limit_headers": {},
        }
        # Verify step: Succeeds
        verify_resp = {
            "status": "ok",
            "status_code": 200,
            "content": "VERIFIED",
            "tokens_in": 100,
            "tokens_out": 5,
            "rate_limit_headers": {},
        }

        fake_client = ScriptedFakeClient([
            understand_resp,
            draft_attempt_1,
            draft_attempt_2,
            verify_resp,
        ])

        limiter = SharedLimiter()
        result = await pipeline.answer(
            question="What is Moshi?",
            deadline=15.0,
            setting_key="B",
            client=fake_client,
            limiter=limiter,
        )

        # Assertions
        step_names = [s.name for s in result.trace.steps]
        print(f"  Executed Steps: {step_names}")

        # Check step counts
        understand_steps = [s for s in result.trace.steps if s.name == "1_understand"]
        retrieve_steps = [s for s in result.trace.steps if s.name == "2_retrieve_tool"]
        draft_steps = [s for s in result.trace.steps if s.name.startswith("4_draft")]

        self.assertEqual(len(understand_steps), 1, "Step 1 (understand) must run exactly once")
        self.assertEqual(len(retrieve_steps), 1, "Step 2 (retrieve) must run exactly once")
        self.assertEqual(len(draft_steps), 1, "Draft step container recorded once")
        self.assertEqual(draft_steps[0].retries, 1, "Draft step executed 1 retry")
        self.assertEqual(result.status, "ok")
        print("  [PASS] Failed draft step retried locally without restarting understand or retrieve.")

    async def test_3_shared_cooldown_across_workers(self):
        """
        PROVE: A 429 response sets a shared cooldown across all workers,
        preventing concurrent stampedes to the model provider.
        """
        print("\n--- Running Test 3: Shared Cooldown Across Workers ---")
        limiter = SharedLimiter()

        # Initial state
        self.assertEqual(limiter.get_remaining_cooldown(), 0.0)

        # Worker A encounters a 429 requiring 1.5s cooldown
        cooldown_needed = 1.5
        limiter.set_shared_cooldown(cooldown_needed)

        remaining_cd = limiter.get_remaining_cooldown()
        self.assertGreater(remaining_cd, 1.0, "Shared cooldown must be active")

        # Worker B enters with remaining deadline = 0.5s (< cooldown)
        # It must be shed immediately without waiting
        t0 = time.perf_counter()
        acquired_b, reason_b, wait_b = await limiter.acquire_slot(
            estimated_tokens=100,
            deadline_remaining=0.5,
        )
        t1 = time.perf_counter()

        self.assertFalse(acquired_b, "Worker B must be rejected due to active shared cooldown")
        self.assertIn("Shared cooldown", reason_b)
        self.assertLess(t1 - t0, 0.1, "Worker B must be shed immediately without sleeping")

        # Worker C enters with remaining deadline = 3.0s (> cooldown)
        # It waits for the shared cooldown and acquires slot successfully
        acquired_c, reason_c, wait_c = await limiter.acquire_slot(
            estimated_tokens=100,
            deadline_remaining=3.0,
        )
        self.assertTrue(acquired_c, "Worker C must acquire slot after cooldown expires")
        limiter.release_slot()
        print(f"  [PASS] Cooldown shared: Worker B shed in {(t1-t0)*1000:.1f}ms; Worker C acquired slot after cooldown.")


if __name__ == "__main__":
    unittest.main()
