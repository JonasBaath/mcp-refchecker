"""MCP server that wraps academic-refchecker to verify academic citations."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import types
from typing import Any

import requests
from fuzzywuzzy import fuzz
from mcp.server.fastmcp import FastMCP
from refchecker import ArxivReferenceChecker


def _debug(msg: str) -> None:
    if os.environ.get("MCP_REFCHECKER_DEBUG"):
        print(f"[mcp-refchecker debug] {msg}", file=sys.stderr)

OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
FUZZY_MIN_RATIO = 85
FUZZY_CANDIDATE_LIMIT = 5

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
    """Query OpenAlex's search endpoint and find the best fuzzy title match.
    Used as fallback when refchecker's stricter validation returned unverified.
    Catches typos and minor title variations.

    OpenAlex is used instead of Semantic Scholar because it has no strict rate
    limits for unauthenticated use and supports a 'polite pool' via mailto param.

    Returns a dict with matched paper info and similarity score, or None if
    no sufficiently close match was found.
    """
    params: dict[str, Any] = {
        "search": title,
        "per_page": FUZZY_CANDIDATE_LIMIT,
    }
    # Polite pool — provides better access when a contact email is set
    mailto = os.environ.get("OPENALEX_MAILTO")
    if mailto:
        params["mailto"] = mailto

    _debug(f"fuzzy_fallback: querying OpenAlex for '{title}'")
    try:
        response = requests.get(
            OPENALEX_SEARCH_URL,
            params=params,
            timeout=10,
        )
        _debug(f"fuzzy_fallback: status={response.status_code}")
        if response.status_code != 200:
            _debug(f"fuzzy_fallback: non-200 body: {response.text[:300]}")
            return None
        data = response.json() or {}
        candidates = data.get("results") or []
        _debug(f"fuzzy_fallback: got {len(candidates)} candidates")
        if not candidates:
            return None
        # Scan all candidates and pick the one with highest fuzzy ratio
        best_candidate = None
        best_ratio = 0
        title_lower = title.lower()
        for candidate in candidates:
            candidate_title = (candidate.get("title") or "").strip()
            if not candidate_title:
                continue
            ratio = fuzz.ratio(title_lower, candidate_title.lower())
            _debug(f"fuzzy_fallback: candidate '{candidate_title[:60]}' ratio={ratio}")
            if ratio > best_ratio:
                best_ratio = ratio
                best_candidate = candidate
        if best_candidate is None or best_ratio < min_ratio:
            _debug(f"fuzzy_fallback: best ratio {best_ratio} < {min_ratio}, no match")
            return None
        # Extract metadata from OpenAlex's response shape
        authorships = best_candidate.get("authorships") or []
        author_names = []
        for authorship in authorships:
            author = authorship.get("author") or {}
            name = author.get("display_name")
            if name:
                author_names.append(name)
        host_venue = best_candidate.get("host_venue") or {}
        primary_location = best_candidate.get("primary_location") or {}
        source = primary_location.get("source") or {}
        venue = host_venue.get("display_name") or source.get("display_name")
        doi = best_candidate.get("doi") or ""
        url_out = doi if doi else best_candidate.get("id")
        return {
            "title": best_candidate.get("title"),
            "authors": author_names,
            "year": best_candidate.get("publication_year"),
            "venue": venue,
            "url": url_out,
            "similarity": best_ratio,
        }
    except Exception as e:
        _debug(f"fuzzy_fallback: exception {type(e).__name__}: {e}")
        return None


def _is_real_mismatch_warning(w: dict) -> bool:
    """Return True if a warning represents a real metadata conflict that
    should block verification (not a version-related or arxiv-preprint
    benign warning).

    refchecker inconsistently marks year mismatches as warning_type (instead
    of error_type like it does for authors). Promote plain 'year'/'author'
    warnings with 'mismatch' in the details to hard errors.
    """
    wtype = w.get("warning_type", "")
    details = (w.get("warning_details") or "").lower()
    # Version-related differences are benign (arxiv v1 vs v2, etc.)
    if "(v" in wtype:
        return False
    # arXiv preprint vs published venue is benign metadata drift
    if "arxiv preprint" in details:
        return False
    # Plain year/author/venue warnings with "mismatch" = real conflict
    if wtype in ("year", "author", "venue") and "mismatch" in details:
        return True
    return False


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
            # Promote real year/author/venue mismatches (marked as warnings by
            # refchecker) to hard errors — they indicate real metadata conflict.
            if _is_real_mismatch_warning(e):
                hard_errors.append(e)
            else:
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
