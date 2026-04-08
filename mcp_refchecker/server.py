"""MCP server that wraps academic-refchecker to verify academic citations."""
from __future__ import annotations

import asyncio
import json
import os
import types
from typing import Any

from mcp.server.fastmcp import FastMCP
from refchecker import ArxivReferenceChecker

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

    result: dict[str, Any] = {
        "verified": errors is None,
        "url": paper_url,
        "matched_paper": None,
        "errors": errors,
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
