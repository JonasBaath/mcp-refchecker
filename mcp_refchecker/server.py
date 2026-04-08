"""MCP server that wraps academic-refchecker to verify academic citations."""
from __future__ import annotations

import asyncio
import json
import os
import types
from typing import Any

import requests
from fuzzywuzzy import fuzz
from mcp.server.fastmcp import FastMCP
from refchecker import ArxivReferenceChecker

S2_SEARCH_MATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search/match"
FUZZY_MIN_RATIO = 85

mcp = FastMCP("mcp-refchecker")

_checker: ArxivReferenceChecker | None = None
_checker_lock = asyncio.Lock()


async def _get_checker() -> ArxivReferenceChecker:
    global _checker
    async with _checker_lock:
        if _checker is None:
            _checker = ArxivReferenceChecker(
                semantic_scholar_api_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY"),
                llm_config={"disabled": True},
            )
    return _checker


def _build_reference(
    title: str,
    authors: list[str] | None,
    year: int | None,
    doi: str | None,
    arxiv_id: str | None,
    url: str | None,
) -> dict[str, Any]:
    # Build raw_text — used by verify_reference_standard for logging/fallback paths
    parts: list[str] = []
    if authors:
        parts.append(", ".join(authors))
    if year:
        parts.append(f"({year})")
    parts.append(title)
    ref: dict[str, Any] = {"title": title, "raw_text": " ".join(parts)}
    if authors:
        ref["authors"] = authors
    if year:
        ref["year"] = year
    if doi:
        ref["doi"] = doi
    if arxiv_id:
        ref["arxiv_id"] = arxiv_id
    # Auto-fill url from doi or arxiv_id so CrossRef/ArXiv checkers can extract IDs
    if url:
        ref["url"] = url
    elif doi:
        ref["url"] = f"https://doi.org/{doi}"
    elif arxiv_id:
        ref["url"] = f"https://arxiv.org/abs/{arxiv_id}"
    return ref


def _fuzzy_fallback(title: str, min_ratio: int = FUZZY_MIN_RATIO) -> dict | None:
    """Query Semantic Scholar's search/match endpoint directly to find a close
    fuzzy match when refchecker's stricter validation returned unverified.
    Catches single-letter typos and minor title variations.

    Returns a dict with matched paper info and similarity score, or None if
    no sufficiently close match was found.
    """
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    headers = {"x-api-key": api_key} if api_key else {}
    try:
        response = requests.get(
            S2_SEARCH_MATCH_URL,
            params={
                "query": title,
                "fields": "title,authors,year,venue,externalIds,url",
            },
            headers=headers,
            timeout=10,
        )
        if response.status_code != 200:
            return None
        data = response.json() or {}
        candidates = data.get("data") or []
        if not candidates:
            return None
        best = candidates[0]
        best_title = (best.get("title") or "").strip()
        if not best_title:
            return None
        similarity = fuzz.ratio(title.lower(), best_title.lower())
        if similarity < min_ratio:
            return None
        authors_raw = best.get("authors") or []
        return {
            "title": best_title,
            "authors": [a.get("name") for a in authors_raw if a.get("name")],
            "year": best.get("year"),
            "venue": best.get("venue"),
            "url": best.get("url"),
            "similarity": similarity,
        }
    except Exception:
        return None


def _stub_source_paper() -> types.SimpleNamespace:
    """Minimal source_paper stub — only used for logging inside verify_reference."""
    return types.SimpleNamespace(
        title="Verification Request",
        authors=[],
        published=types.SimpleNamespace(year=None),
        canonical_url=None,
        external_paper_id=None,
        venue=None,
    )


@mcp.tool()
async def verify_citation(
    title: str,
    authors: list[str] | None = None,
    year: int | None = None,
    doi: str | None = None,
    arxiv_id: str | None = None,
    url: str | None = None,
) -> str:
    """Verify that an academic citation is accurate by checking it against
    Semantic Scholar, OpenAlex, and CrossRef. Returns whether the paper
    exists and flags mismatches in title, authors, year, or venue.

    Args:
        title: Title of the cited paper (required).
        authors: List of author names.
        year: Publication year.
        doi: DOI of the paper (e.g. 10.1145/12345).
        arxiv_id: arXiv ID (e.g. 2301.00001).
        url: Direct URL to the paper.
    """
    reference = _build_reference(title, authors, year, doi, arxiv_id, url)
    source_paper = _stub_source_paper()

    checker = await _get_checker()
    errors, paper_url, verified_data = await asyncio.to_thread(
        checker.verify_reference, source_paper, reference
    )

    paper_found = verified_data is not None
    hard_errors: list[dict] = []
    warnings: list[dict] = []
    info: list[dict] = []

    for e in errors or []:
        if "info_type" in e:
            info.append(e)
        elif "warning_type" in e:
            warnings.append(e)
        elif "error_type" in e:
            # If paper was found and the error is only that a field was missing
            # in the input (not wrong), treat as a warning — missing input metadata
            # is not proof of hallucination.
            if paper_found and "missing" in e.get("error_details", "").lower():
                warnings.append(e)
            else:
                hard_errors.append(e)

    # Fuzzy fallback: if refchecker returned "unverified", try a direct fuzzy
    # match against Semantic Scholar's search/match endpoint. Catches typos
    # that refchecker's stricter validation rejects.
    possible_match: dict | None = None
    unverified = any(e.get("error_type") == "unverified" for e in hard_errors)
    if not paper_found and unverified:
        possible_match = await asyncio.to_thread(_fuzzy_fallback, title)
        if possible_match:
            warnings.append({
                "warning_type": "fuzzy_match",
                "warning_details": (
                    f"Exact title not found. Closest match in Semantic Scholar: "
                    f"'{possible_match['title']}' "
                    f"(similarity: {possible_match['similarity']}%). "
                    f"Verify whether this is the intended reference."
                ),
            })

    result: dict[str, Any] = {
        "verified": len(hard_errors) == 0,
        "url": paper_url,
        "matched_paper": None,
        "possible_match": possible_match,
        "errors": hard_errors or None,
        "warnings": warnings or None,
        "info": info or None,
    }

    if isinstance(verified_data, dict):
        result["matched_paper"] = {
            "title": verified_data.get("title"),
            "authors": verified_data.get("authors"),
            "year": verified_data.get("year"),
            "venue": verified_data.get("venue"),
        }

    return json.dumps(result, indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
