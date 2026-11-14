"""
pipeline.py - 5-Step Agentic Q&A Pipeline over arXiv Library with Rate Limiting & Verification.

Steps:
1) Understand: Small model analyzes query, extracts search terms, flags off-topic.
   (Skipped under Stage 1+ pressure).
2) Retrieve: Calls retriever as a TOOL with 1.5s timeout and 1 retry.
   (Uses smaller chunk setting under Stage 2+ pressure).
3) Decide (Branch): If top retrieval score < threshold, refuse WITHOUT calling LLM.
4) Draft: Bigger model drafts answer strictly from retrieved passages with citations.
5) Verify (Loop, max 2 rounds): Free code check (citations + numbers) then LLM check.
   (LLM check skipped under Stage 3+ pressure).

Limiter & Policy Integration:
- In-flight concurrency cap and sliding window RPM/TPM accounting via SharedLimiter.
- 429 Retry-After > remaining deadline -> Shed/Degrade immediately (no waiting).
- Shared 429 cooldown prevents stampedes.
- Retries isolated to the failed step (NEVER restarts whole pipeline).
- Full jitter on 5xx/timeouts, no retry on 4xx.
- Daily-limit detection flips shared kill flag.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import (
    DRAFT_MODEL,
    GROQ_MODEL,
    MAX_VERIFY_ROUNDS,
    RETRIEVAL_SCORE_THRESHOLD,
    RETRIEVAL_TOOL_TIMEOUT,
    STREAM_AFTER_VERIFICATION,
    UNDERSTAND_MODEL,
    VERIFY_MODEL,
)
from groq_client import GroqClient
from limiter import GLOBAL_LIMITER, SharedLimiter, execute_step_with_retry
import retrieve


@dataclass
class StepTrace:
    name: str
    start_time: float
    end_time: float
    wait_ms: float = 0.0
    work_ms: float = 0.0
    retry_ms: float = 0.0
    retries: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineTrace:
    question: str
    start_time: float
    end_time: float = 0.0
    steps: List[StepTrace] = field(default_factory=list)
    total_wait_ms: float = 0.0
    total_work_ms: float = 0.0
    total_retry_ms: float = 0.0
    total_ms: float = 0.0
    first_token_ready_time: float = 0.0
    delay_before_first_token_ms: float = 0.0
    delay_after_first_token_ms: float = 0.0
    total_tokens_in: int = 0
    total_tokens_out: int = 0

    def finalize(self) -> None:
        if self.end_time == 0.0:
            self.end_time = time.perf_counter()
        self.total_wait_ms = round(sum(s.wait_ms for s in self.steps), 2)
        self.total_work_ms = round(sum(s.work_ms for s in self.steps), 2)
        self.total_retry_ms = round(sum(s.retry_ms for s in self.steps), 2)
        self.total_ms = round((self.end_time - self.start_time) * 1000, 2)

        if self.first_token_ready_time > 0.0:
            self.delay_before_first_token_ms = round(
                (self.first_token_ready_time - self.start_time) * 1000, 2
            )
            self.delay_after_first_token_ms = round(
                (self.end_time - self.first_token_ready_time) * 1000, 2
            )
        else:
            self.delay_before_first_token_ms = self.total_ms
            self.delay_after_first_token_ms = 0.0

    def to_dict(self) -> Dict[str, Any]:
        self.finalize()
        return {
            "question": self.question,
            "total_ms": self.total_ms,
            "total_wait_ms": self.total_wait_ms,
            "total_work_ms": self.total_work_ms,
            "total_retry_ms": self.total_retry_ms,
            "delay_before_first_token_ms": self.delay_before_first_token_ms,
            "delay_after_first_token_ms": self.delay_after_first_token_ms,
            "total_tokens_in": self.total_tokens_in,
            "total_tokens_out": self.total_tokens_out,
            "steps": [asdict(s) for s in self.steps],
        }


@dataclass
class PipelineResult:
    answer: str
    status: str  # 'ok', 'refused_off_topic', 'refused_low_confidence', 'refused_verification_failed', 'refused_daily_budget_exhausted', 'rate_limited', 'timeout', 'error'
    status_code: int
    trace: PipelineTrace
    citations: List[str]
    retrieved_hits: List[Dict[str, Any]]
    rounds_used: int
    degraded_stage: int = 0


def free_code_check(
    draft: str, retrieved_hits: List[Dict[str, Any]]
) -> Tuple[bool, str, List[str]]:
    """
    Step 5A: Zero-cost structural verification in code.
    1. Checks that citations are present and refer to actually retrieved passages.
    2. Checks that specific claimed numbers exist in the evidence.
    """
    if "papers do not say" in draft.lower() or "not mentioned in the papers" in draft.lower():
        return True, "Valid refusal", []

    # 1. Citations Check
    found_citations = re.findall(r"\[(Attention|Moshi|DynChunk),\s*§[^,\]]+,\s*p\.\s*\d+\]", draft, re.IGNORECASE)
    all_bracket_cites = re.findall(r"\[([^\]]+,\s*§?[^,\]]+,\s*p\.\s*\d+)\]", draft)

    if not found_citations and not all_bracket_cites:
        return False, "Draft contains no citations. Every claim must cite [Paper, §Section, p. Page].", []

    valid_citations = [hit.get("citation", "").strip() for hit in retrieved_hits]
    valid_citations_normalized = [c.lower().replace(" ", "") for c in valid_citations]

    for cite in all_bracket_cites:
        full_cite = f"[{cite}]"
        norm = full_cite.lower().replace(" ", "")
        match_found = any(norm in v or v in norm for v in valid_citations_normalized)
        if not match_found:
            pg_match = re.search(r"p\.\s*(\d+)", cite)
            paper_match = re.search(r"^(Attention|Moshi|DynChunk)", cite, re.IGNORECASE)
            if pg_match and paper_match:
                pg_num = int(pg_match.group(1))
                pap = paper_match.group(1).lower()
                evidence_pages = [(h["paper"].lower(), h["page"]) for h in retrieved_hits]
                if not any(pap in e_pap and pg_num == e_pg for e_pap, e_pg in evidence_pages):
                    return False, f"Hallucinated citation {full_cite} not found in retrieved passages.", all_bracket_cites

    # 2. Numeric Claims Check
    draft_no_cites = re.sub(r"\[[^\]]+\]", " ", draft)
    claimed_numbers = re.findall(r"\b\d+(?:\.\d+)?%?\b", draft_no_cites)
    evidence_text = " ".join(hit.get("text", "") for hit in retrieved_hits)

    for num in claimed_numbers:
        if num in ("1", "2", "3", "4", "5") and len(num) == 1:
            continue
        clean_num = num.rstrip("%")
        if clean_num not in evidence_text and num not in evidence_text:
            return (
                False,
                f"Numeric figure '{num}' claimed in draft does not appear in retrieved evidence.",
                all_bracket_cites,
            )

    return True, "Code check passed", all_bracket_cites


async def retrieve_tool(
    query: str,
    setting_key: str,
    timeout: float = RETRIEVAL_TOOL_TIMEOUT,
) -> Tuple[List[Dict[str, Any]], float, float, int]:
    """
    Retriever as a Tool with strict 1.5s timeout and one retry.
    Returns (hits, work_ms, retry_ms, retries).
    """
    t_start = time.perf_counter()
    retries = 0
    retry_ms = 0.0

    for attempt in range(1, 3):
        att_start = time.perf_counter()
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(retrieve.search_with_setting, query, setting_key),
                timeout=timeout,
            )
            att_end = time.perf_counter()
            work_ms = round((att_end - att_start) * 1000, 2)
            return hits, work_ms, retry_ms, retries
        except (asyncio.TimeoutError, Exception):
            att_end = time.perf_counter()
            if attempt == 1:
                retries += 1
                retry_ms = round((att_end - att_start) * 1000, 2)
                await asyncio.sleep(0.05)
            else:
                return [], round((att_end - t_start) * 1000, 2), retry_ms, retries

    return [], round((time.perf_counter() - t_start) * 1000, 2), retry_ms, retries


async def answer(
    question: str,
    deadline: float = 30.0,
    setting_key: str = "B",
    client: Optional[GroqClient] = None,
    limiter: Optional[SharedLimiter] = None,
) -> PipelineResult:
    """
    Main 5-Step Agentic Pipeline with Rate Limiting, Degraded Modes, and Gated Delivery.
    """
    t_global_start = time.perf_counter()
    trace = PipelineTrace(question=question, start_time=t_global_start)
    groq = client or GroqClient()
    lim = limiter or GLOBAL_LIMITER

    def get_remaining_deadline() -> float:
        elapsed = time.perf_counter() - t_global_start
        return max(0.1, deadline - elapsed)

    # Check daily budget exhaustion
    if lim.is_daily_exhausted():
        trace.finalize()
        refusal_msg = "Service Unavailable: Daily API budget has been exhausted. No further requests can be processed until reset."
        return PipelineResult(
            answer=refusal_msg,
            status="refused_daily_budget_exhausted",
            status_code=429,
            trace=trace,
            citations=[],
            retrieved_hits=[],
            rounds_used=0,
            degraded_stage=3,
        )

    # Check pressure and stage degradation
    degrade_stage = lim.get_pressure_stage()
    effective_setting = "A" if degrade_stage >= 2 else setting_key

    # =========================================================================
    # STEP 1: UNDERSTAND
    # =========================================================================
    is_off_topic = False
    search_query = question

    if degrade_stage >= 1:
        # Under pressure: Skip understand step to save latency & tokens
        trace.steps.append(
            StepTrace(
                name="1_understand_skipped_pressure",
                start_time=time.perf_counter(),
                end_time=time.perf_counter(),
                metadata={"degrade_stage": degrade_stage, "action": "used raw query directly"},
            )
        )
    else:
        rem_dl = get_remaining_deadline()
        if rem_dl <= 0.3:
            trace.finalize()
            return PipelineResult(
                answer="Request timed out before processing.",
                status="timeout",
                status_code=408,
                trace=trace,
                citations=[],
                retrieved_hits=[],
                rounds_used=0,
                degraded_stage=degrade_stage,
            )

        understand_prompt = [
            {
                "role": "system",
                "content": (
                    "You are an assistant query analyzer for a research library containing 3 papers:\n"
                    "1. Attention Is All You Need\n"
                    "2. Moshi: Speech-text foundation model for real-time dialogue\n"
                    "3. Dynamic Chunking for End-to-End Hierarchical Sequence Modeling\n"
                    "Respond in JSON format: {\"is_off_topic\": bool, \"search_query\": str}\n"
                    "If clearly off-topic, set is_off_topic: true."
                ),
            },
            {"role": "user", "content": question},
        ]

        async def do_understand():
            return await groq.stream_chat(
                messages=understand_prompt,
                max_tokens=500,
                model=UNDERSTAND_MODEL,
                timeout=get_remaining_deadline(),
            )

        t_step_start = time.perf_counter()
        und_res, und_work_ms, und_retry_ms, und_retries = await execute_step_with_retry(
            step_name="1_understand",
            coro_fn=do_understand,
            deadline_remaining_fn=get_remaining_deadline,
            limiter=lim,
            estimated_tokens=200,
        )
        t_step_end = time.perf_counter()

        trace.total_tokens_in += und_res.get("tokens_in", 0)
        trace.total_tokens_out += und_res.get("tokens_out", 0)

        # Handle rate limit shedding
        if und_res.get("status") == "rate_limited" or und_res.get("status_code") == 429:
            trace.finalize()
            return PipelineResult(
                answer=f"Request shed: {und_res.get('error_body')}",
                status="rate_limited",
                status_code=429,
                trace=trace,
                citations=[],
                retrieved_hits=[],
                rounds_used=0,
                degraded_stage=degrade_stage,
            )

        # Parse JSON
        content_txt = und_res.get("content", "")
        try:
            json_match = re.search(r"\{.*\}", content_txt, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
                is_off_topic = bool(data.get("is_off_topic", False))
                search_query = data.get("search_query", question).strip() or question
        except Exception:
            pass

        trace.steps.append(
            StepTrace(
                name="1_understand",
                start_time=t_step_start,
                end_time=t_step_end,
                work_ms=und_work_ms,
                retry_ms=und_retry_ms,
                retries=und_retries,
                metadata={"search_query": search_query, "is_off_topic": is_off_topic},
            )
        )

        if is_off_topic:
            trace.finalize()
            refusal_msg = (
                "I can only answer questions grounded in the 3 indexed papers: 'Attention Is All You Need', "
                "'Moshi', and 'Dynamic Chunking'. This question appears outside that scope."
            )
            return PipelineResult(
                answer=refusal_msg,
                status="refused_correctly",
                status_code=200,
                trace=trace,
                citations=[],
                retrieved_hits=[],
                rounds_used=0,
                degraded_stage=degrade_stage,
            )

    # =========================================================================
    # STEP 2: RETRIEVE (Tool call with 1.5s timeout + 1 retry)
    # =========================================================================
    rem_dl = get_remaining_deadline()
    t_step_start = time.perf_counter()
    hits, ret_work_ms, ret_retry_ms, ret_retries = await retrieve_tool(
        query=search_query,
        setting_key=effective_setting,
        timeout=min(RETRIEVAL_TOOL_TIMEOUT, rem_dl),
    )
    t_step_end = time.perf_counter()

    trace.steps.append(
        StepTrace(
            name="2_retrieve_tool",
            start_time=t_step_start,
            end_time=t_step_end,
            work_ms=ret_work_ms,
            retry_ms=ret_retry_ms,
            retries=ret_retries,
            metadata={"hits_count": len(hits), "top_score": hits[0]["score"] if hits else 0.0, "setting": effective_setting},
        )
    )

    # =========================================================================
    # STEP 3: DECIDE (Branch based on retrieval threshold)
    # =========================================================================
    t_step_start = time.perf_counter()
    if hits:
        # If hits were reranked (blended score in [0, 1]), use raw bm25_score for the threshold
        top_score = hits[0].get("bm25_score", hits[0]["score"])
    else:
        top_score = 0.0
    refused_low_confidence = (not hits) or (top_score < RETRIEVAL_SCORE_THRESHOLD)
    t_step_end = time.perf_counter()

    trace.steps.append(
        StepTrace(
            name="3_decide_branch",
            start_time=t_step_start,
            end_time=t_step_end,
            work_ms=round((t_step_end - t_step_start) * 1000, 2),
            metadata={"top_score": top_score, "threshold": RETRIEVAL_SCORE_THRESHOLD, "refused": refused_low_confidence},
        )
    )

    if refused_low_confidence:
        trace.finalize()
        refusal_msg = (
            f"The papers do not say. (Retrieval confidence score {top_score:.2f} "
            f"was below the required threshold of {RETRIEVAL_SCORE_THRESHOLD:.2f})."
        )
        return PipelineResult(
            answer=refusal_msg,
            status="refused_correctly",
            status_code=200,
            trace=trace,
            citations=[],
            retrieved_hits=hits,
            rounds_used=0,
            degraded_stage=degrade_stage,
        )

    # Format retrieved evidence
    evidence_blocks = []
    for i, h in enumerate(hits, 1):
        evidence_blocks.append(f"Passage {i} {h['citation']}:\n{h['text']}\n")
    evidence_str = "\n".join(evidence_blocks)

    # =========================================================================
    # STEP 4 & 5: DRAFT & VERIFY LOOP (Max 2 Rounds)
    # =========================================================================
    draft_answer = ""
    valid_citations = []
    critique_feedback = ""
    rounds_executed = 0

    for round_num in range(1, MAX_VERIFY_ROUNDS + 1):
        rounds_executed = round_num

        # STEP 4: DRAFT
        rem_dl = get_remaining_deadline()
        if rem_dl <= 0.5:
            trace.finalize()
            return PipelineResult(
                answer="Request deadline exceeded during synthesis.",
                status="timeout",
                status_code=408,
                trace=trace,
                citations=[],
                retrieved_hits=hits,
                rounds_used=rounds_executed,
                degraded_stage=degrade_stage,
            )

        draft_messages = [
            {
                "role": "system",
                "content": (
                    "You are a precise technical assistant answering questions about research papers.\n"
                    "RULES:\n"
                    "1. Answer ONLY using the facts stated in the EXCERPTS below.\n"
                    "2. Every factual statement MUST cite where it came from using the passage citation format, e.g. [Attention, §3.2, p. 4].\n"
                    "3. If the excerpts do not contain the answer, say 'The papers do not say.' Do NOT speculate.\n"
                    "4. If numbers or hyperparameters are mentioned, quote them exactly as written."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"EXCERPTS:\n{evidence_str}\n\n"
                    f"QUESTION: {question}\n\n"
                    + (f"NOTE: Your previous attempt was rejected: {critique_feedback}. Correct it.\n" if critique_feedback else "")
                    + "Provide a cited, clear answer:"
                ),
            },
        ]

        async def do_draft():
            return await groq.stream_chat(
                messages=draft_messages,
                max_tokens=700,
                model=DRAFT_MODEL,
                timeout=get_remaining_deadline(),
            )

        t_draft_start = time.perf_counter()
        draft_res, draft_work_ms, draft_retry_ms, draft_retries = await execute_step_with_retry(
            step_name=f"4_draft_round_{round_num}",
            coro_fn=do_draft,
            deadline_remaining_fn=get_remaining_deadline,
            limiter=lim,
            estimated_tokens=500,
        )
        t_draft_end = time.perf_counter()

        trace.total_tokens_in += draft_res.get("tokens_in", 0)
        trace.total_tokens_out += draft_res.get("tokens_out", 0)
        draft_answer = draft_res.get("content", "").strip()

        trace.steps.append(
            StepTrace(
                name=f"4_draft_round_{round_num}",
                start_time=t_draft_start,
                end_time=t_draft_end,
                work_ms=draft_work_ms,
                retry_ms=draft_retry_ms,
                retries=draft_retries,
                metadata={"tokens_out": draft_res.get("tokens_out", 0)},
            )
        )

        if draft_res.get("status") == "rate_limited" or draft_res.get("status_code") == 429:
            # Shed or degrade
            trace.finalize()
            return PipelineResult(
                answer=f"Request shed: {draft_res.get('error_body')}",
                status="rate_limited",
                status_code=429,
                trace=trace,
                citations=[],
                retrieved_hits=hits,
                rounds_used=rounds_executed,
                degraded_stage=degrade_stage,
            )

        # STEP 5A: FREE CODE CHECK
        t_chk_start = time.perf_counter()
        code_ok, code_msg, cites = free_code_check(draft_answer, hits)
        t_chk_end = time.perf_counter()

        trace.steps.append(
            StepTrace(
                name=f"5_verify_code_round_{round_num}",
                start_time=t_chk_start,
                end_time=t_chk_end,
                work_ms=round((t_chk_end - t_chk_start) * 1000, 2),
                metadata={"passed": code_ok, "details": code_msg},
            )
        )

        if not code_ok:
            critique_feedback = code_msg
            if round_num < MAX_VERIFY_ROUNDS:
                continue
            else:
                trace.finalize()
                refusal_msg = f"Refused: The answer could not be verified against the papers ({code_msg})."
                return PipelineResult(
                    answer=refusal_msg,
                    status="refused_verification_failed",
                    status_code=200,
                    trace=trace,
                    citations=[],
                    retrieved_hits=hits,
                    rounds_used=rounds_executed,
                    degraded_stage=degrade_stage,
                )

        valid_citations = cites

        # STEP 5B: LLM FACTUALITY AUDIT CHECK
        if degrade_stage >= 3:
            # Under severe pressure: skip LLM check and rely on passed code check
            trace.steps.append(
                StepTrace(
                    name=f"5_verify_llm_skipped_pressure_round_{round_num}",
                    start_time=time.perf_counter(),
                    end_time=time.perf_counter(),
                    metadata={"degrade_stage": degrade_stage, "action": "accepted passed code check"},
                )
            )
            break

        rem_dl = get_remaining_deadline()
        if rem_dl <= 0.3:
            break

        audit_prompt = [
            {
                "role": "system",
                "content": (
                    "You are a rigorous scientific reviewer. Check if the DRAFT ANSWER is faithfully supported by the EVIDENCE.\n"
                    "If all claims and citations are supported, reply 'VERIFIED'.\n"
                    "If the draft contains hallucinations, numbers not in evidence, or unsupported claims, reply 'REJECTED: <reason>'."
                ),
            },
            {
                "role": "user",
                "content": f"EVIDENCE:\n{evidence_str}\n\nDRAFT ANSWER:\n{draft_answer}\n\nReview:",
            },
        ]

        async def do_audit():
            return await groq.stream_chat(
                messages=audit_prompt,
                max_tokens=400,
                model=VERIFY_MODEL,
                timeout=get_remaining_deadline(),
            )

        t_audit_start = time.perf_counter()
        audit_res, audit_work_ms, audit_retry_ms, audit_retries = await execute_step_with_retry(
            step_name=f"5_verify_llm_round_{round_num}",
            coro_fn=do_audit,
            deadline_remaining_fn=get_remaining_deadline,
            limiter=lim,
            estimated_tokens=200,
        )
        t_audit_end = time.perf_counter()

        trace.total_tokens_in += audit_res.get("tokens_in", 0)
        trace.total_tokens_out += audit_res.get("tokens_out", 0)
        audit_verdict = audit_res.get("content", "").strip()

        is_verified = audit_verdict.upper().startswith("VERIFIED")
        trace.steps.append(
            StepTrace(
                name=f"5_verify_llm_round_{round_num}",
                start_time=t_audit_start,
                end_time=t_audit_end,
                work_ms=audit_work_ms,
                retry_ms=audit_retry_ms,
                retries=audit_retries,
                metadata={"verdict": audit_verdict[:80], "verified": is_verified},
            )
        )

        if is_verified:
            break
        else:
            critique_feedback = audit_verdict
            if round_num < MAX_VERIFY_ROUNDS:
                continue
            else:
                trace.finalize()
                refusal_msg = f"Refused: The answer failed verification review ({audit_verdict})."
                return PipelineResult(
                    answer=refusal_msg,
                    status="refused_verification_failed",
                    status_code=200,
                    trace=trace,
                    citations=[],
                    retrieved_hits=hits,
                    rounds_used=rounds_executed,
                    degraded_stage=degrade_stage,
                )

    # GATED DELIVERY: Tokens dispatched only after complete verification
    t_ready = time.perf_counter()
    trace.first_token_ready_time = t_ready
    trace.finalize()

    return PipelineResult(
        answer=draft_answer,
        status="ok",
        status_code=200,
        trace=trace,
        citations=valid_citations,
        retrieved_hits=hits,
        rounds_used=rounds_executed,
        degraded_stage=degrade_stage,
    )


def print_trace_summary(res: PipelineResult) -> None:
    t = res.trace
    print("\n" + "=" * 70)
    print("PIPELINE EXECUTION TRACE")
    print("=" * 70)
    print(f"Status: {res.status.upper()} (HTTP {res.status_code})")
    print(f"Degraded Mode Stage: {res.degraded_stage}")
    print(f"Total Latency: {t.total_ms:.1f} ms")
    print(f"  - Total Working Time: {t.total_work_ms:.1f} ms")
    print(f"  - Total Waiting Time: {t.total_wait_ms:.1f} ms")
    print(f"  - Total Retry Time:   {t.total_retry_ms:.1f} ms")
    print(f"Delay Breakdown (Option 2: Verified Gated Delivery):")
    print(f"  - Before First User Token: {t.delay_before_first_token_ms:.1f} ms (synthesis + verification)")
    print(f"  - After First User Token:  {t.delay_after_first_token_ms:.1f} ms")
    print(f"Total Tokens: In={t.total_tokens_in} | Out={t.total_tokens_out}")
    print(f"Verification Rounds: {res.rounds_used}")

    print("\nStep-by-Step Latency Breakdown:")
    print(f"  {'Step':<32} | {'Work (ms)':<10} | {'Wait (ms)':<10} | {'Retry (ms)':<10} | {'Retries'}")
    print("  " + "-" * 72)
    for s in t.steps:
        print(f"  {s.name:<32} | {s.work_ms:<10.1f} | {s.wait_ms:<10.1f} | {s.retry_ms:<10.1f} | {s.retries}")


async def main():
    parser = argparse.ArgumentParser(description="Run 5-step agentic Q&A pipeline over research papers.")
    parser.add_argument("question", type=str, help="Question to answer")
    parser.add_argument("--deadline", type=float, default=30.0, help="User deadline in seconds (default: 30.0)")
    parser.add_argument("--setting", type=str, default="B", choices=["A", "B", "C"], help="Retrieval setting (default: B)")
    args = parser.parse_args()

    print(f"\nUser Question: \"{args.question}\"")
    print(f"Deadline: {args.deadline}s | Retrieval Setting: {args.setting}\n")

    res = await answer(question=args.question, deadline=args.deadline, setting_key=args.setting)

    print("\n" + "=" * 70)
    print("FINAL VERIFIED ANSWER:")
    print("=" * 70)
    print(res.answer)

    print_trace_summary(res)


if __name__ == "__main__":
    asyncio.run(main())
