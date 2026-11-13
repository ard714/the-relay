"""
retrieve.py - Chunking and passage retrieval over the 3 arXiv research papers.

Supports BM25 lexical ranking and TF-IDF cosine re-ranking.
Preserves paper, section, and page metadata with exact citation formatting.

Usage:
    python retrieve.py "What is the key idea behind Moshi?" --setting B
"""

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from rank_bm25 import BM25Okapi
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from config import (
    DEFAULT_RETRIEVAL_SETTING,
    PAPER_DISPLAY_NAMES,
    RERANK_BM25_WEIGHT,
    RERANK_TFIDF_WEIGHT,
    RETRIEVAL_SETTINGS,
)

PAGES_JSONL_PATH = Path("library/pages.jsonl")


def format_citation(paper_key: str, section: str, page: int) -> str:
    """Format citation string: [Paper, §Section, p. Page], e.g. [Moshi, §3.2, p. 7]."""
    display_paper = PAPER_DISPLAY_NAMES.get(paper_key.lower(), paper_key.capitalize())

    # Extract concise section identifier if available (e.g., '3.2' from '3.2 Overview')
    sec_clean = section.strip()
    match = re.match(r"^((?:[1-9]\d*|[A-Z])(?:\.\d+)*)\s*", sec_clean)
    if match:
        sec_str = f"§{match.group(1)}"
    elif sec_clean.lower() in ("abstract", "title / abstract"):
        sec_str = "§Abstract"
    elif sec_clean.lower() == "references":
        sec_str = "§References"
    elif sec_clean:
        sec_str = f"§{sec_clean[:25]}"
    else:
        sec_str = "§General"

    return f"[{display_paper}, {sec_str}, p. {page}]"


def tokenize_for_bm25(text: str) -> List[str]:
    """Tokenize text into lowercase alphanumeric words."""
    return re.findall(r"\b[a-zA-Z0-9_\-\.]+\b", text.lower())


class DocumentIndex:
    """In-memory index maintaining chunk metadata, BM25 index, and TF-IDF vectorizer."""

    def __init__(
        self,
        chunks: List[Dict[str, Any]],
        bm25: BM25Okapi,
        tfidf_vectorizer: TfidfVectorizer,
        tfidf_matrix: Any,
    ):
        self.chunks = chunks
        self.bm25 = bm25
        self.tfidf_vectorizer = tfidf_vectorizer
        self.tfidf_matrix = tfidf_matrix

    def search(
        self,
        query: str,
        top_k: int = 5,
        rerank: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Search indexed chunks.
        If rerank=False: returns top_k by BM25.
        If rerank=True: takes top 2*k by BM25, re-scores with weighted BM25 + TF-IDF cosine, keeps top_k.
        """
        query_tokens = tokenize_for_bm25(query)
        if not query_tokens:
            return []

        # Step 1: Compute BM25 scores
        bm25_scores = np.array(self.bm25.get_scores(query_tokens))

        if not rerank:
            # Plain BM25 top-k
            top_indices = np.argsort(bm25_scores)[::-1][:top_k]
            results = []
            for idx in top_indices:
                chunk = self.chunks[idx].copy()
                chunk["score"] = round(float(bm25_scores[idx]), 4)
                results.append(chunk)
            return results

        # Step 2: Rerank top 2*k candidates with TF-IDF Cosine
        pool_k = min(len(self.chunks), top_k * 2)
        candidate_indices = np.argsort(bm25_scores)[::-1][:pool_k]

        if len(candidate_indices) == 0:
            return []

        candidate_bm25 = bm25_scores[candidate_indices]
        # Min-max normalization for BM25 scores across candidates
        min_b = np.min(candidate_bm25)
        max_b = np.max(candidate_bm25)
        if max_b > min_b:
            norm_bm25 = (candidate_bm25 - min_b) / (max_b - min_b)
        else:
            norm_bm25 = np.ones_like(candidate_bm25)

        # Compute TF-IDF cosine similarity for candidate chunks
        query_vec = self.tfidf_vectorizer.transform([query])
        cand_matrix = self.tfidf_matrix[candidate_indices]
        cosine_sims = cosine_similarity(query_vec, cand_matrix).flatten()

        # Weighted blend
        final_scores = (
            RERANK_BM25_WEIGHT * norm_bm25 + RERANK_TFIDF_WEIGHT * cosine_sims
        )

        ranked_cand_order = np.argsort(final_scores)[::-1][:top_k]

        results = []
        for r_idx in ranked_cand_order:
            actual_idx = candidate_indices[r_idx]
            chunk = self.chunks[actual_idx].copy()
            chunk["score"] = round(float(final_scores[r_idx]), 4)
            chunk["bm25_score"] = round(float(candidate_bm25[r_idx]), 4)
            chunk["cosine_score"] = round(float(cosine_sims[r_idx]), 4)
            results.append(chunk)

        return results


def chunk_page_text(
    text: str, chunk_tokens: int, overlap: int
) -> List[str]:
    """Split page text into chunks of roughly chunk_tokens words with overlap."""
    words = text.split()
    if not words:
        return []
    if len(words) <= chunk_tokens:
        return [" ".join(words)]

    step = max(1, chunk_tokens - overlap)
    chunks = []
    for i in range(0, len(words), step):
        chunk_words = words[i : i + chunk_tokens]
        if chunk_words:
            chunks.append(" ".join(chunk_words))
        if i + chunk_tokens >= len(words):
            break
    return chunks


def build_index(
    chunk_tokens: int = 280,
    overlap: int = 50,
    pages_path: Path = PAGES_JSONL_PATH,
) -> DocumentIndex:
    """Build in-memory BM25 and TF-IDF index from pages.jsonl."""
    if not pages_path.exists():
        raise FileNotFoundError(
            f"{pages_path} does not exist. Run 'python parse_papers.py' first."
        )

    chunks: List[Dict[str, Any]] = []
    corpus_tokenized: List[List[str]] = []
    corpus_texts: List[str] = []

    with open(pages_path, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            paper = rec["paper"]
            page = rec["page"]
            heading = rec.get("heading", "General")
            text = rec.get("text", "")

            # Create chunks for this page
            page_chunks = chunk_page_text(text, chunk_tokens=chunk_tokens, overlap=overlap)
            for c_text in page_chunks:
                clean_chunk = c_text.strip()
                if len(clean_chunk) < 30:
                    continue
                citation = format_citation(paper, heading, page)
                chunk_dict = {
                    "text": clean_chunk,
                    "paper": paper,
                    "section": heading,
                    "page": page,
                    "citation": citation,
                }
                chunks.append(chunk_dict)
                tokens = tokenize_for_bm25(clean_chunk)
                corpus_tokenized.append(tokens)
                corpus_texts.append(clean_chunk)

    if not chunks:
        raise ValueError("No chunks were extracted from pages.jsonl.")

    # Build BM25 index
    bm25 = BM25Okapi(corpus_tokenized)

    # Build TF-IDF vectorizer and matrix
    tfidf_vectorizer = TfidfVectorizer(
        lowercase=True,
        stop_words="english",
        token_pattern=r"\b[a-zA-Z0-9_\-\.]+\b",
    )
    tfidf_matrix = tfidf_vectorizer.fit_transform(corpus_texts)

    return DocumentIndex(chunks, bm25, tfidf_vectorizer, tfidf_matrix)


# Cache indices per (chunk_tokens, overlap) tuple to avoid rebuilding on every query
_INDEX_CACHE: Dict[Tuple[int, int], DocumentIndex] = {}


def get_cached_index(chunk_tokens: int, overlap: int) -> DocumentIndex:
    key = (chunk_tokens, overlap)
    if key not in _INDEX_CACHE:
        _INDEX_CACHE[key] = build_index(chunk_tokens=chunk_tokens, overlap=overlap)
    return _INDEX_CACHE[key]


def search(
    query: str,
    top_k: int = 5,
    rerank: bool = False,
    chunk_tokens: int = 280,
    overlap: int = 50,
) -> List[Dict[str, Any]]:
    """Convenience search function using cached index."""
    idx = get_cached_index(chunk_tokens=chunk_tokens, overlap=overlap)
    return idx.search(query=query, top_k=top_k, rerank=rerank)


def search_with_setting(
    query: str, setting_key: str = DEFAULT_RETRIEVAL_SETTING
) -> List[Dict[str, Any]]:
    """Search using a predefined named setting from config.py ('A', 'B', or 'C')."""
    setting = RETRIEVAL_SETTINGS.get(setting_key.upper())
    if not setting:
        raise ValueError(
            f"Unknown setting '{setting_key}'. Available: {list(RETRIEVAL_SETTINGS.keys())}"
        )

    return search(
        query=query,
        top_k=setting["top_k"],
        rerank=setting["rerank"],
        chunk_tokens=setting["chunk_tokens"],
        overlap=setting["overlap"],
    )


def main():
    parser = argparse.ArgumentParser(
        description="Search paper passages using BM25 and TF-IDF cosine re-ranking."
    )
    parser.add_argument("query", type=str, help="Search question or query")
    parser.add_argument(
        "--setting",
        type=str,
        default=DEFAULT_RETRIEVAL_SETTING,
        choices=["A", "B", "C"],
        help=f"Retrieval setting (default: {DEFAULT_RETRIEVAL_SETTING})",
    )
    args = parser.parse_args()

    setting = RETRIEVAL_SETTINGS[args.setting]
    print(f"\nSearching for: \"{args.query}\"")
    print(f"Setting {args.setting}: {setting['description']}\n")

    hits = search_with_setting(args.query, setting_key=args.setting)

    print(f"Retrieved {len(hits)} passages:\n" + "-" * 70)
    for i, hit in enumerate(hits, 1):
        print(f"Hit {i} | Score: {hit['score']} | Citation: {hit['citation']}")
        print(f"Paper: {hit['paper']} | Section: {hit['section']} | Page: {hit['page']}")
        preview = hit["text"][:220].replace("\n", " ")
        print(f"Excerpt: \"{preview}...\"\n")


if __name__ == "__main__":
    main()
