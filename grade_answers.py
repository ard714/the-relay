"""
grade_answers.py - First-pass automated answer grader with side-by-side display and hand-overrule support.

Reads questions from ques.yaml (or questions.json) and answers from answers.jsonl,
computes a first-pass auto-grade:
  - correct
  - wrong
  - refused_correctly
  - refused_wrongly
Prints every graded answer next to its expected answer and required criteria.
Supports overruling auto-grades interactively or via an overrides YAML file,
and can optionally update results.csv.
"""

import argparse
import csv
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

import yaml

VALID_GRADES = {"correct", "wrong", "refused_correctly", "refused_wrongly"}

REFUSAL_MARKERS = [
    "papers do not say",
    "outside that scope",
    "cannot be answered",
    "not stated in the paper",
    "does not state",
    "does not mention",
    "does not evaluate",
    "not measured",
    "unanswerable",
    "refused:",
]


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


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


def load_answers(filepath: str = "answers.jsonl") -> List[Dict[str, Any]]:
    path = Path(filepath)
    if not path.exists():
        return []

    answers = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    answers.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return answers


def load_overrides(filepath: Optional[str]) -> Dict[str, str]:
    if not filepath or not Path(filepath).exists():
        return {}
    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {k.strip(): str(v).strip().lower() for k, v in data.items()}


def auto_grade_single(
    q_obj: Dict[str, Any],
    ans_obj: Dict[str, Any],
) -> Tuple[str, List[str], List[str]]:
    """
    Compute first-pass grade.
    Returns: (grade, matched_phrases, missing_phrases)
    """
    expect = str(q_obj.get("expect", "")).lower().strip()
    if not expect:
        is_ans = q_obj.get("answerable", True)
        expect = "answer" if is_ans else "refuse"

    must_contain = q_obj.get("must_contain") or q_obj.get("expected_keywords") or []
    if isinstance(must_contain, str):
        must_contain = [must_contain]

    ans_text = ans_obj.get("answer", "")
    ans_norm = normalize_text(ans_text)
    status = str(ans_obj.get("status", "")).lower()

    # Determine if the pipeline produced a refusal
    is_refusal = any(m in ans_norm for m in REFUSAL_MARKERS) or status.startswith("refused")

    matched = []
    missing = []
    for p in must_contain:
        if normalize_text(p) in ans_norm:
            matched.append(p)
        else:
            missing.append(p)

    has_citations = len(ans_obj.get("citations", [])) > 0 or re.search(r"\[(?:Attention|Moshi|DynChunk)[^\]]+\]", ans_text) is not None

    if expect == "refuse":
        if is_refusal:
            grade = "refused_correctly"
        else:
            grade = "wrong"  # Hallucinated answer for an unanswerable question
    else:  # expect == "answer"
        if is_refusal:
            grade = "refused_wrongly"
        else:
            # Need required phrases (or keywords) and citations
            if must_contain:
                match_ratio = len(matched) / len(must_contain)
                if match_ratio >= 0.5 and has_citations:
                    grade = "correct"
                elif match_ratio >= 0.5:
                    grade = "correct"  # Has core facts
                else:
                    grade = "wrong"
            else:
                grade = "correct" if has_citations else "wrong"

    return grade, matched, missing


def run_grader(
    questions: List[Dict[str, Any]],
    answers: List[Dict[str, Any]],
    overrides: Dict[str, str],
    interactive: bool = False,
) -> List[Dict[str, Any]]:
    graded_items = []

    print("\n" + "=" * 90)
    print("FIRST-PASS ANSWER GRADING & SIDE-BY-SIDE REVIEW")
    print("=" * 90)

    # Index answers by question_id and question text
    ans_by_id = {}
    ans_by_qtext = {}
    for a in answers:
        if a.get("question_id"):
            ans_by_id[a["question_id"]] = a
        if a.get("question"):
            ans_by_qtext[normalize_text(a["question"])] = a

    for idx, q_obj in enumerate(questions, 1):
        q_id = q_obj.get("id", f"q{idx:02d}")
        q_text = q_obj.get("question", "").strip()
        paper = q_obj.get("paper", "Unknown")
        expect = str(q_obj.get("expect", "")).lower().strip()
        if not expect:
            expect = "answer" if q_obj.get("answerable", True) else "refuse"

        must_contain = q_obj.get("must_contain") or q_obj.get("expected_keywords") or []
        if isinstance(must_contain, str):
            must_contain = [must_contain]

        ground_truth = q_obj.get("ground_truth", "")

        # Find matching answer
        ans_obj = ans_by_id.get(q_id) or ans_by_qtext.get(normalize_text(q_text))
        if not ans_obj:
            ans_obj = {
                "request_id": f"unmatched_{q_id}",
                "answer": "[NO ANSWER LOGGED IN answers.jsonl]",
                "citations": [],
                "status": "missing",
            }

        auto_grade, matched_phrases, missing_phrases = auto_grade_single(q_obj, ans_obj)

        final_grade = auto_grade
        overruled = False

        # Apply file override if present
        if q_id in overrides:
            cand = overrides[q_id]
            if cand in VALID_GRADES:
                final_grade = cand
                overruled = True

        print(f"\n------------------------------------------------------------------------------------------")
        print(f"[{idx:02d}/{len(questions)}] ID: {q_id} | Paper: {paper} | Expectation: [{expect.upper()}]")
        print(f"------------------------------------------------------------------------------------------")
        print(f"QUESTION:  \"{q_text}\"")
        if ground_truth:
            print(f"GROUND TRUTH: \"{ground_truth}\"")
        print(f"CRITERIA:  Must contain: {must_contain}")
        print(f"\nSYSTEM ANSWER:")
        print(f"  \"{ans_obj.get('answer', '').strip()}\"")
        print(f"  Citations: {ans_obj.get('citations', [])}")
        print(f"  Status:    {ans_obj.get('status')} | Matched: {matched_phrases} | Missing: {missing_phrases}")

        grade_status_str = f"AUTO-GRADE: [{auto_grade.upper()}]"
        if overruled:
            grade_status_str += f" -> OVERRULED BY FILE TO: [{final_grade.upper()}]"
        print(f"\n--> {grade_status_str}")

        if interactive:
            prompt_str = (
                f"Enter final grade [Enter=keep ({final_grade}) / c=correct / w=wrong / rc=refused_correctly / rw=refused_wrongly]: "
            )
            choice = input(prompt_str).strip().lower()
            mapping = {
                "c": "correct",
                "w": "wrong",
                "rc": "refused_correctly",
                "rw": "refused_wrongly",
            }
            if choice in mapping:
                final_grade = mapping[choice]
                overruled = True
                print(f"    Manual overrule applied: [{final_grade.upper()}]")

        graded_items.append({
            "id": q_id,
            "question": q_text,
            "paper": paper,
            "expect": expect,
            "must_contain": must_contain,
            "ground_truth": ground_truth,
            "answer": ans_obj.get("answer", ""),
            "citations": ans_obj.get("citations", []),
            "auto_grade": auto_grade,
            "final_grade": final_grade,
            "overruled": overruled,
            "request_id": ans_obj.get("request_id"),
        })

    # Summary
    print("\n" + "=" * 90)
    print("FINAL GRADING SUMMARY:")
    print("=" * 90)
    counts = {}
    for g in graded_items:
        counts[g["final_grade"]] = counts.get(g["final_grade"], 0) + 1
    for k, v in counts.items():
        print(f"  {k:<20}: {v:>3} ({v/len(graded_items)*100:>5.1f}%)")
    overrule_count = sum(1 for g in graded_items if g["overruled"])
    print(f"  Total Overrules Applied: {overrule_count}")
    print("=" * 90 + "\n")

    return graded_items


def update_results_csv(graded_items: List[Dict[str, Any]], csv_path: str = "results.csv") -> int:
    path = Path(csv_path)
    if not path.exists():
        return 0

    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys())

    grade_map = {g["request_id"]: g["final_grade"] for g in graded_items if g.get("request_id")}

    updated = 0
    for r in rows:
        req_id = r.get("request_id")
        if req_id in grade_map:
            r["answer_grade"] = grade_map[req_id]
            updated += 1

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return updated


def main():
    parser = argparse.ArgumentParser(description="First-pass answer grader with side-by-side display and overruling.")
    parser.add_argument("--config", default="ques.yaml", help="Path to ques.yaml or questions.json")
    parser.add_argument("--answers", default="answers.jsonl", help="Path to answers.jsonl")
    parser.add_argument("--overrides", default=None, help="Optional YAML file with manual overrides ({q_id: grade})")
    parser.add_argument("--interactive", action="store_true", help="Interactively prompt to confirm or overrule each grade")
    parser.add_argument("--out-grades", default="grades.json", help="Path to save reviewed grades JSON")
    parser.add_argument("--update-csv", action="store_true", help="Update results.csv with the reviewed grades")
    parser.add_argument("--csv", default="results.csv", help="Path to results.csv")

    args = parser.parse_args()

    questions = load_yaml_questions(args.config)
    answers = load_answers(args.answers)
    overrides = load_overrides(args.overrides)

    graded = run_grader(questions, answers, overrides, interactive=args.interactive)

    # Save to out_grades
    with open(args.out_grades, "w", encoding="utf-8") as f:
        json.dump(graded, f, indent=2)
    print(f"Saved graded results to '{args.out_grades}'")

    if args.update_csv:
        n = update_results_csv(graded, args.csv)
        print(f"Updated {n} row(s) in '{args.csv}' with final reviewed grades.")


if __name__ == "__main__":
    main()

