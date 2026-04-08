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

CROSSREF_SEARCH_URL = "https://api.crossref.org/works"
CROSSREF_USER_AGENT_BASE = "mcp-refchecker/0.1.0"
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


def _crossref_year(item: dict) -> int | None:
    """Extract publication year from a Crossref item. Tries published-print,
    published-online, issued, and created in order."""
    for key in ("published-print", "published-online", "issued", "created"):
        entry = item.get(key) or {}
        parts = entry.get("date-parts") or []
        if parts and parts[0]:
            year = parts[0][0]
            if isinstance(year, int):
                return year
    return None


def _crossref_authors(item: dict) -> list[str]:
    """Extract author names from a Crossref item as 'Given Family' strings."""
    authors = item.get("author") or []
    names = []
    for a in authors:
        given = a.get("given", "")
        family = a.get("family", "")
        name = f"{given} {family}".strip()
        if name:
            names.append(name)
    return names


def _fuzzy_fallback(title: str, min_ratio: int = FUZZY_MIN_RATIO) -> dict | None:
    """Query Crossref's /works endpoint and return the best title match above
    min_ratio. Used as fallback when refchecker's stricter validation returned
    unverified.

    Scope: catches stylistic title variations (case differences, punctuation,
    word order, minor rewording) — NOT real typos in distinctive title words.
    Free academic search APIs do keyword/token matching, so a misspelled word
    simply isn't in the index. For a title like "Atention Is All You Need",
    Crossref returns papers matching "All You Need" but not the real paper.
    Catching typos would require semantic embeddings (paid API).

    Crossref is used because it's free, requires no API key, is authoritative
    for DOI metadata, and supports a polite pool via mailto in User-Agent.
    Set CROSSREF_MAILTO env var to use the polite pool for better performance.

    Returns a dict with matched paper info and similarity score, or None if
    no sufficiently close match was found.
    """
    mailto = os.environ.get("CROSSREF_MAILTO")
    user_agent = (
        f"{CROSSREF_USER_AGENT_BASE} (mailto:{mailto})"
        if mailto else CROSSREF_USER_AGENT_BASE
    )
    headers = {"User-Agent": user_agent}
    params = {
        "query.title": title,
        "rows": FUZZY_CANDIDATE_LIMIT,
        "select": "DOI,title,author,published-print,published-online,issued,created,container-title,URL",
    }

    _debug(f"fuzzy_fallback: querying Crossref for '{title}'")
    try:
        response = requests.get(
            CROSSREF_SEARCH_URL,
            params=params,
            headers=headers,
            timeout=10,
        )
        _debug(f"fuzzy_fallback: status={response.status_code}")
        if response.status_code != 200:
            _debug(f"fuzzy_fallback: non-200 body: {response.text[:300]}")
            return None
        data = response.json() or {}
        message = data.get("message") or {}
        candidates = message.get("items") or []
        _debug(f"fuzzy_fallback: got {len(candidates)} candidates")
        if not candidates:
            return None
        # Scan all candidates and pick the one with highest fuzzy ratio
        best_candidate = None
        best_ratio = 0
        title_lower = title.lower()
        for candidate in candidates:
            titles = candidate.get("title") or []
            candidate_title = (titles[0] if titles else "").strip()
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
        titles = best_candidate.get("title") or []
        container_title = best_candidate.get("container-title") or []
        return {
            "title": titles[0] if titles else None,
            "authors": _crossref_authors(best_candidate),
            "year": _crossref_year(best_candidate),
            "venue": container_title[0] if container_title else None,
            "url": best_candidate.get("URL"),
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

    # Fuzzy fallback: if refchecker returned "unverified", try a secondary
    # Crossref search to catch stylistic title variations (case, punctuation,
    # word order) that refchecker's strict comparison may have rejected.
    # NOTE: this does NOT catch real typos in distinctive words — see
    # _fuzzy_fallback docstring.
    possible_match: dict | None = None
    unverified = any(e.get("error_type") == "unverified" for e in hard_errors)
    if not paper_found and unverified:
        possible_match = await asyncio.to_thread(_fuzzy_fallback, title)
        if possible_match:
            warnings.append({
                "warning_type": "fuzzy_match",
                "warning_details": (
                    f"Exact title not found. Closest match in Crossref: "
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
