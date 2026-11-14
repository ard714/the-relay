"""
check_questions.py - Verify question ground truth against original paper PDFs.

Loads questions from ques.yaml (or questions.json), searches the cited paper's PDF text
for every string in `must_contain`, and confirms that each string actually appears in the paper.
Lists any missing strings loudly so ground-truth errors can be caught and fixed.
"""

import argparse
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import pymupdf  # PyMuPDF
import yaml

PAPER_PDFS = {
    "attention": "library/1706.03762v7.pdf",
    "moshi": "library/2410.00037v1.pdf",
    "dynchunk": "library/2507.07955v2.pdf",
    "dynamic chunking": "library/2507.07955v2.pdf",
    "h-net": "library/2507.07955v2.pdf",
}


def normalize_text(text: str) -> str:
    """Normalize whitespace and lowercase for robust substring matching."""
    # Replace newlines, non-breaking spaces, and multi-spaces with single space
    return re.sub(r"\s+", " ", text).strip().lower()


class PDFLibrary:
    """Loads and caches page-level text for all three papers."""

    def __init__(self):
        self.papers: Dict[str, List[Tuple[int, str, str]]] = {}  # key -> list of (page_num, raw_text, norm_text)
        self._load_papers()

    def _load_papers(self):
        for key, path_str in PAPER_PDFS.items():
            if key in self.papers:
                continue
            path = Path(path_str)
            if not path.exists():
                print(f"Warning: PDF file '{path_str}' not found.")
                continue

            doc = pymupdf.open(path)
            pages = []
            for page_idx in range(len(doc)):
                page = doc[page_idx]
                raw = page.get_text()
                norm = normalize_text(raw)
                pages.append((page_idx + 1, raw, norm))
            self.papers[key] = pages

    def search_string(self, query_str: str, paper_key: str) -> List[Tuple[str, int, str]]:
        """
        Search for query_str in the specified paper(s).
        Returns list of (paper_name, page_number, matching_snippet).
        """
        query_norm = normalize_text(query_str)
        if not query_norm:
            return []

        target_keys = []
        p_lower = paper_key.lower().strip()
        if "cross" in p_lower:
            target_keys = ["attention", "moshi", "dynchunk"]
        elif "attention" in p_lower:
            target_keys = ["attention"]
        elif "moshi" in p_lower:
            target_keys = ["moshi"]
        elif "chunk" in p_lower or "dyn" in p_lower or "h-net" in p_lower:
            target_keys = ["dynchunk"]
        elif "off-topic" in p_lower:
            target_keys = ["attention", "moshi", "dynchunk"]
        else:
            target_keys = ["attention", "moshi", "dynchunk"]

        matches = []
        for pk in target_keys:
            pages = self.papers.get(pk, [])
            for page_num, raw, norm in pages:
                if query_norm in norm:
                    # Find approximate snippet in raw text
                    idx = norm.find(query_norm)
                    start = max(0, idx - 40)
                    end = min(len(raw), idx + len(query_norm) + 40)
                    snippet = raw[start:end].replace("\n", " ").strip()
                    matches.append((pk, page_num, snippet))

        return matches


def load_yaml_questions(filepath: str = "ques.yaml") -> List[Dict[str, Any]]:
    path = Path(filepath)
    if not path.exists():
        raise FileNotFoundError(f"Questions file '{filepath}' not found.")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if isinstance(data, dict) and "questions" in data:
        data = data["questions"]

    if not isinstance(data, list):
        raise ValueError(f"Expected a list of questions in '{filepath}', got {type(data)}")

    return data


def verify_questions(questions: List[Dict[str, Any]], library: PDFLibrary) -> Dict[str, Any]:
    total_phrases = 0
    passed_phrases = 0
    failed_phrases = 0

    results = []

    for idx, q_obj in enumerate(questions, 1):
        q_id = q_obj.get("id", f"q{idx:02d}")
        q_text = q_obj.get("question", "").strip()
        paper = q_obj.get("paper", "Attention")
        expect = str(q_obj.get("expect", "")).lower().strip()
        if not expect:
            # Fallback to answerable flag
            is_ans = q_obj.get("answerable", True)
            expect = "answer" if is_ans else "refuse"

        must_contain = q_obj.get("must_contain") or q_obj.get("expected_keywords") or []
        if isinstance(must_contain, str):
            must_contain = [must_contain]

        q_report = {
            "id": q_id,
            "question": q_text,
            "paper": paper,
            "expect": expect,
            "must_contain": must_contain,
            "passed": True,
            "phrase_results": [],
        }

        if expect == "answer":
            if not must_contain:
                q_report["warning"] = "No must_contain strings specified for answerable question."

            for phrase in must_contain:
                total_phrases += 1
                matches = library.search_string(phrase, paper)
                if matches:
                    passed_phrases += 1
                    pages_str = ", ".join(f"p. {m[1]} ({m[0]})" for m in matches[:3])
                    q_report["phrase_results"].append({
                        "phrase": phrase,
                        "status": "PASS",
                        "pages": pages_str,
                        "matches_count": len(matches),
                    })
                else:
                    failed_phrases += 1
                    q_report["passed"] = False
                    q_report["phrase_results"].append({
                        "phrase": phrase,
                        "status": "FAIL",
                        "pages": None,
                        "matches_count": 0,
                    })

        elif expect == "refuse":
            # For unanswerable questions, verify that candidate keywords do NOT appear in the paper
            for phrase in must_contain:
                total_phrases += 1
                matches = library.search_string(phrase, paper)
                if matches:
                    # Found in paper when it was expected to NOT appear
                    q_report["phrase_results"].append({
                        "phrase": phrase,
                        "status": "FLAG_FOUND",
                        "note": f"Found on {len(matches)} page(s) (e.g. p. {matches[0][1]}), verify if question is truly unanswerable.",
                        "matches_count": len(matches),
                    })
                else:
                    passed_phrases += 1
                    q_report["phrase_results"].append({
                        "phrase": phrase,
                        "status": "PASS_ABSENT",
                        "note": "Confirmed absent from cited paper.",
                        "matches_count": 0,
                    })

        results.append(q_report)

    return {
        "total_questions": len(questions),
        "total_phrases": total_phrases,
        "passed_phrases": passed_phrases,
        "failed_phrases": failed_phrases,
        "reports": results,
    }


def print_verification_report(summary: Dict[str, Any]) -> None:
    print("\n" + "=" * 90)
    print("GROUND TRUTH VERIFICATION REPORT (check_questions.py)")
    print("=" * 90)
    print(f"Total Questions Evaluated: {summary['total_questions']}")
    print(f"Total Phrases Checked:     {summary['total_phrases']}")
    print(f"Passed Phrases:            {summary['passed_phrases']}")
    print(f"Failed / Missing Phrases:  {summary['failed_phrases']}")
    print("-" * 90)

    failed_questions = []

    for r in summary["reports"]:
        q_id = r["id"]
        expect = r["expect"]
        paper = r["paper"]
        phrases = r["phrase_results"]
        has_fail = any(p["status"] == "FAIL" for p in phrases)

        if has_fail:
            failed_questions.append(r)
            status_icon = "[FAIL]"
        elif r.get("warning"):
            status_icon = "[WARN]"
        else:
            status_icon = "[PASS]"

        print(f"\n{status_icon} {q_id} | Paper: {paper} | Expect: {expect}")
        print(f"      Q: \"{r['question'][:70]}...\"" if len(r["question"]) > 70 else f"      Q: \"{r['question']}\"")

        if r.get("warning"):
            print(f"      Warning: {r['warning']}")

        for pr in phrases:
            if pr["status"] == "PASS":
                print(f"      - [OK]   \"{pr['phrase']}\" -> Found on {pr['pages']}")
            elif pr["status"] == "PASS_ABSENT":
                print(f"      - [OK]   \"{pr['phrase']}\" -> {pr['note']}")
            elif pr["status"] == "FLAG_FOUND":
                print(f"      - [FLAG] \"{pr['phrase']}\" -> {pr['note']}")
            elif pr["status"] == "FAIL":
                print(f"      - [MISSING] \"{pr['phrase']}\" NOT FOUND in cited paper text!")

    print("\n" + "=" * 90)
    if summary["failed_phrases"] == 0:
        print("[SUCCESS] All must_contain strings verified against the cited paper PDFs!")
    else:
        print(f"[ACTION REQUIRED] {summary['failed_phrases']} must_contain phrase(s) failed verification across {len(failed_questions)} question(s).")
        print("Please check the missing phrases above and correct typos or terminology in ques.yaml.")
    print("=" * 90 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Verify question must_contain phrases against PDF library.")
    parser.add_argument("--config", default="ques.yaml", help="Path to ques.yaml or questions.json")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 if any phrase fails")
    args = parser.parse_args()

    questions = load_yaml_questions(args.config)
    library = PDFLibrary()

    summary = verify_questions(questions, library)
    print_verification_report(summary)

    if args.strict and summary["failed_phrases"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
