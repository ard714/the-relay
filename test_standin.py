"""
test_standin.py - Automated tests verifying stand-in server endpoints and SSE streaming.
"""

import asyncio
import json
import unittest
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
import standin


class TestStandinServer(AioHTTPTestCase):

    async def get_application(self):
        # Initialize app with calibration settings
        test_config = {
            "ttft_p50_ms": 20.0,
            "ttft_p95_ms": 40.0,
            "per_token_ms": 2.0,
            "rpm_limit": 5,
            "tpm_limit": 1000,
            "in_flight_cap": 3,
            "retry_after_seconds": 2.0,
        }
        return standin.create_app(initial_config=test_config)

    @unittest_run_loop
    async def test_1_admin_config_get_and_post(self):
        # 1. Read config
        resp = await self.client.get("/admin/config")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data["rpm_limit"], 5)

        # 2. Update config live
        resp = await self.client.post("/admin/config", json={"rpm_limit": 10, "random_5xx_rate": 0.05})
        self.assertEqual(resp.status, 200)
        updated = await resp.json()
        self.assertEqual(updated["config"]["rpm_limit"], 10)
        self.assertEqual(updated["config"]["random_5xx_rate"], 0.05)
        print("\n  [PASS] /admin/config GET and live POST update verified.")

    @unittest_run_loop
    async def test_2_sse_streaming_chat_completions(self):
        payload = {
            "model": "openai/gpt-oss-20b",
            "messages": [{"role": "user", "content": "What is Moshi?"}],
            "stream": True,
        }
        resp = await self.client.post("/v1/chat/completions", json=payload)
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))

        chunks = []
        async for line in resp.content:
            line_str = line.decode().strip()
            if line_str.startswith("data: "):
                data_part = line_str[6:]
                if data_part == "[DONE]":
                    chunks.append("[DONE]")
                    break
                parsed = json.loads(data_part)
                chunks.append(parsed)

        self.assertGreater(len(chunks), 3)
        self.assertEqual(chunks[-1], "[DONE]")
        # Check usage in second to last chunk
        usage_chunk = chunks[-2]
        self.assertIn("usage", usage_chunk)
        print(f"  [PASS] /v1/chat/completions streamed {len(chunks)} SSE chunks and final usage.")

    @unittest_run_loop
    async def test_3_rate_limiting_429_groq_shape(self):
        # Set low RPM limit
        await self.client.post("/admin/config", json={"rpm_limit": 2, "retry_after_seconds": 2.0})
        await self.client.post("/admin/reset")

        # Request 1 & 2 succeed
        r1 = await self.client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r1.status, 200)
        r2 = await self.client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r2.status, 200)

        # Request 3 triggers 429
        r3 = await self.client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r3.status, 429)
        self.assertEqual(r3.headers.get("retry-after"), "2")

        body = await r3.json()
        self.assertIn("error", body)
        self.assertIn("Rate limit reached for model", body["error"]["message"])
        self.assertEqual(body["error"]["code"], "rate_limit_exceeded")
        print(f"  [PASS] 429 Groq error body and retry-after header matched: {body['error']['message'][:70]}...")

    @unittest_run_loop
    async def test_4_exhaust_day_and_reset(self):
        # Trigger daily exhaustion
        resp = await self.client.post("/admin/exhaust_day")
        self.assertEqual(resp.status, 200)

        # Call must be rejected with RPD 429
        req = await self.client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(req.status, 429)
        body = await req.json()
        self.assertIn("requests per day (RPD)", body["error"]["message"])

        # Reset clears exhaustion
        reset_resp = await self.client.post("/admin/reset")
        self.assertEqual(reset_resp.status, 200)

        req_after = await self.client.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(req_after.status, 200)
        print("  [PASS] /admin/exhaust_day and /admin/reset successfully simulated daily budget shutdown and recovery.")


if __name__ == "__main__":
    unittest.main()

