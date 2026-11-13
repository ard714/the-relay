"""
parse_papers.py - Parse the 3 arXiv research papers using PyMuPDF (fitz).

Produces:
    library/pages.jsonl - one JSON record per page:
        {"paper": "attention", "page": 1, "heading": "Title / Abstract", "text": "..."}

Prints:
    - Page count and list of discovered headings per paper
    - Top 5 pages with the worst extraction quality (short text, heavy symbols, tables)
"""

import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF

PAPERS = [
    {
        "short_name": "attention",
        "title": "Attention Is All You Need",
        "path": Path("library/1706.03762v7.pdf"),
    },
    {
        "short_name": "moshi",
        "title": "Moshi: a speech-text foundation model for real-time dialogue",
        "path": Path("library/2410.00037v1.pdf"),
    },
    {
        "short_name": "dynchunk",
        "title": "Dynamic Chunking for End-to-End Hierarchical Sequence Modeling",
        "path": Path("library/2507.07955v2.pdf"),
    },
]

# Section heading regexes
NUMBERED_PREFIX_RE = re.compile(
    r"^(?:(?:[1-9]\d*|[IVXLCDM]+|[A-Z])(?:\.\d+)*\.?)$"
)
FULL_HEADING_RE = re.compile(
    r"^(?:(?:[1-9]\d*|[IVXLCDM]+|[A-Z])(?:\.\d+)*\.?)\s+[A-Z][A-Za-z0-9\s\-,:–—/()]{2,70}$"
)
STANDARD_HEADINGS = {
    "abstract",
    "introduction",
    "background",
    "related work",
    "model architecture",
    "architecture",
    "experiments",
    "experimental setup",
    "results",
    "discussion",
    "conclusion",
    "conclusions",
    "future work",
    "references",
    "acknowledgments",
    "acknowledgements",
    "broader impact",
    "appendix",
}

BOLD_FONT_HINTS = [
    "bold",
    "medi",
    "demi",
    "black",
    "heavy",
    "semibol",
    "linlibertinetb",
    "bld",
    "tb",
]

AUTHORS_FILTER = {
    "ashish vaswani", "noam shazeer", "niki parmar", "jakob uszkoreit",
    "llion jones", "aidan n. gomez", "łukasz kaiser", "illia polosukhin",
    "sukjun hwang", "brandon wang", "albert gu", "alexandre défense",
    "laurent mazaré", "manu vashishtha", "eugène marin", "arthur szlam"
}


def is_running_header_or_footer(line_text: str) -> bool:
    line = line_text.strip()
    if not line:
        return True
    if line.isdigit() and len(line) <= 3:
        return True
    if re.search(r"arxiv:\d{4}\.\d{4,5}(v\d+)?", line, re.IGNORECASE):
        return True
    if re.search(r"under review|preprint|published as|proceedings of", line, re.IGNORECASE):
        return True
    return False


def is_bold_span(span: Dict[str, Any]) -> bool:
    font_name = span.get("font", "").lower()
    flags = span.get("flags", 0)
    if bool(flags & 2) or bool(flags & 16):
        return True
    return any(h in font_name for h in BOLD_FONT_HINTS)


def estimate_body_font_size(doc: fitz.Document) -> float:
    font_sizes = Counter()
    for page in doc:
        page_dict = page.get_text("dict")
        for block in page_dict.get("blocks", []):
            if "lines" not in block:
                continue
            for line in block["lines"]:
                for span in line.get("spans", []):
                    text = span.get("text", "").strip()
                    if len(text) > 4:
                        size = round(span.get("size", 10.0), 1)
                        font_sizes[size] += len(text)
    if font_sizes:
        return font_sizes.most_common(1)[0][0]
    return 10.0


def extract_candidate_lines(page: fitz.Page) -> List[Dict[str, Any]]:
    """Extract ordered lines with font metadata from a page."""
    lines_meta = []
    page_dict = page.get_text("dict")
    for block in page_dict.get("blocks", []):
        if "lines" not in block:
            continue
        for line in block["lines"]:
            spans = line.get("spans", [])
            if not spans:
                continue
            line_text = " ".join(s.get("text", "").strip() for s in spans if s.get("text", "").strip()).strip()
            if not line_text or is_running_header_or_footer(line_text):
                continue

            first_span = spans[0]
            max_size = max(s.get("size", 10.0) for s in spans)
            any_bold = any(is_bold_span(s) for s in spans)
            y_pos = first_span.get("bbox", [0, 0, 0, 0])[1]

            lines_meta.append({
                "text": line_text,
                "y_pos": y_pos,
                "font_size": max_size,
                "is_bold": any_bold,
                "spans": spans,
            })

    lines_meta.sort(key=lambda x: x["y_pos"])
    return lines_meta


def detect_headings_on_page(page: fitz.Page, body_size: float) -> List[Tuple[float, str]]:
    """Detect section headings on a page, handling two-line split numbered headings."""
    candidates = extract_candidate_lines(page)
    headings: List[Tuple[float, str]] = []
    skip_next = False

    for i in range(len(candidates)):
        if skip_next:
            skip_next = False
            continue

        curr = candidates[i]
        curr_text = curr["text"]
        curr_clean = curr_text.strip()
        lower_text = curr_clean.lower()
        is_prominent = curr["is_bold"] or (curr["font_size"] >= body_size * 1.10)

        # Ignore multi-number table rows (e.g. '28.4 41.8' or '57.4 71.7')
        if re.search(r"\d+\.\d+\s+[\d\.]+", curr_clean) or re.search(r"\d+%\s+\d+", curr_clean):
            continue

        # Ignore single isolated letters like 'Q Q' or 'N d model'
        if re.match(r"^[A-Za-z](\s+[A-Za-z]){1,3}$", curr_clean):
            continue

        # Ignore lines with no lowercase letters that are not standard uppercase Roman numerals
        if curr_clean.isupper() and len(curr_clean) > 4 and curr_clean.lower() not in STANDARD_HEADINGS:
            # allow standard acronyms like 'FLOPs' or 'DC' but not table headers
            if not any(h in curr_clean for h in ["FLOP", "DC", "AR", "NAR"]):
                continue
        if any(auth in lower_text for auth in AUTHORS_FILTER):
            continue

        # Case 1: Standard headings (e.g. Abstract, Introduction, References)
        if lower_text in STANDARD_HEADINGS and is_prominent:
            headings.append((curr["y_pos"], curr_clean))
            continue

        # Case 2: Already combined heading (e.g. "3.2 Attention" or "1 Introduction")
        if FULL_HEADING_RE.match(curr_clean) and (is_prominent or curr["font_size"] >= body_size):
            headings.append((curr["y_pos"], curr_clean))
            continue

        # Case 3: Split heading (Line 1: "3.2" or "A", Line 2: "Attention" or "Related Work")
        if NUMBERED_PREFIX_RE.match(curr_clean) and is_prominent and (i + 1 < len(candidates)):
            next_cand = candidates[i + 1]
            next_text = next_cand["text"].strip()
            next_lower = next_text.lower()
            if (
                next_cand["is_bold"] or (next_cand["font_size"] >= body_size * 1.05)
            ) and len(next_text) <= 70 and not next_text.endswith(".") and not re.match(r"^[\d\.\s%/\-–—]+$", next_text):
                if not any(auth in next_lower for auth in AUTHORS_FILTER):
                    combined = f"{curr_clean} {next_text}"
                    headings.append((curr["y_pos"], combined))
                    skip_next = True
                    continue

    return headings


def compute_page_defect_score(text: str) -> Tuple[float, str]:
    stripped = text.strip()
    total_len = len(stripped)
    if total_len == 0:
        return 999.0, "Empty page"

    symbol_count = sum(1 for c in stripped if not c.isalnum() and not c.isspace())
    digit_count = sum(1 for c in stripped if c.isdigit())
    symbol_ratio = symbol_count / total_len
    digit_ratio = digit_count / total_len

    lines = [l.strip() for l in stripped.splitlines() if l.strip()]

    # Detect table-like lines (lines with multiple numbers or columnar data)
    table_lines = 0
    for l in lines:
        tokens = l.split()
        if len(tokens) >= 3 and sum(1 for t in tokens if re.search(r"^\d", t)) >= 2:
            table_lines += 1

    reasons = []
    score = 0.0

    # 1. Sparse / figure-heavy pages
    if total_len < 1000:
        short_pts = (1000 - total_len) * 0.02
        score += short_pts
        reasons.append(f"Sparse / figure-heavy text ({total_len} chars)")

    # 2. Dense numeric data (tables, benchmark metrics)
    if digit_ratio > 0.05:
        num_pts = (digit_ratio - 0.05) * 150.0
        score += num_pts
        reasons.append(f"High numeric density ({digit_ratio*100:.1f}%)")

    # 3. Dense math formulas and punctuation soup
    if symbol_ratio > 0.06:
        sym_pts = (symbol_ratio - 0.06) * 120.0
        score += sym_pts
        reasons.append(f"Heavy formulas/symbols ({symbol_ratio*100:.1f}%)")

    # 4. Table structure
    if table_lines >= 3:
        score += table_lines * 1.5
        reasons.append(f"Table rows ({table_lines} rows)")

    reason_str = ", ".join(reasons) if reasons else "Clean prose"
    return round(score, 2), reason_str


def parse_all_papers(output_path: Path = Path("library/pages.jsonl")) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_pages_records = []
    paper_reports = []

    for paper in PAPERS:
        short_name = paper["short_name"]
        pdf_path = paper["path"]

        if not pdf_path.exists():
            print(f"ERROR: {pdf_path} not found!", file=sys.stderr)
            continue

        doc = fitz.open(pdf_path)
        body_size = estimate_body_font_size(doc)

        current_heading = "Title / Abstract"
        headings_found_with_pages = []
        page_defect_records = []

        for page_idx in range(len(doc)):
            page_num = page_idx + 1
            page = doc[page_idx]

            heading_at_top = current_heading

            page_headings = detect_headings_on_page(page, body_size)
            for _, h_text in page_headings:
                if not any(h[1] == h_text for h in headings_found_with_pages):
                    headings_found_with_pages.append((page_num, h_text))
                current_heading = h_text

            text = page.get_text("text")

            record = {
                "paper": short_name,
                "page": page_num,
                "heading": heading_at_top,
                "text": text,
            }
            all_pages_records.append(record)

            score, reason = compute_page_defect_score(text)
            page_defect_records.append(
                {
                    "page": page_num,
                    "score": score,
                    "reason": reason,
                    "char_count": len(text.strip()),
                    "heading_at_top": heading_at_top,
                }
            )

        page_defect_records.sort(key=lambda x: x["score"], reverse=True)
        top5_worst = page_defect_records[:5]

        paper_reports.append(
            {
                "short_name": short_name,
                "title": paper["title"],
                "total_pages": len(doc),
                "headings": headings_found_with_pages,
                "worst_pages": top5_worst,
            }
        )

    with open(output_path, "w", encoding="utf-8") as f:
        for rec in all_pages_records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Wrote {len(all_pages_records)} pages to {output_path}\n")

    print("=" * 80)
    print("PAPER EXTRACTION & QUALITY REPORT")
    print("=" * 80)

    for rep in paper_reports:
        print(f"\n[{rep['short_name'].upper()}] {rep['title']}")
        print(f"Total Pages: {rep['total_pages']}")
        print(f"Discovered Headings ({len(rep['headings'])}):")
        for pg, h in rep['headings']:
            print(f"  Page {pg:2d}: {h}")

        print("\nTop 5 Pages with Worst / Most Challenging Extraction:")
        for w in rep['worst_pages']:
            print(
                f"  Page {w['page']:2d} | Score: {w['score']:6.1f} | Chars: {w['char_count']:4d} | "
                f"Section: '{w['heading_at_top'][:28]}' | Issues: {w['reason']}"
            )
        print("-" * 80)


if __name__ == "__main__":
    parse_all_papers()
