"""Tests for mcp_refchecker.server.verify_citation.

Kör: pytest tests/ --cov=mcp_refchecker --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import mcp_refchecker.server as srv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fake_to_thread(func, *args, **kwargs):
    """Ersätter asyncio.to_thread i tester — kör synkront i event-loopen."""
    return func(*args, **kwargs)


def _call(
    title,
    authors=None,
    year=None,
    doi=None,
    arxiv_id=None,
    url=None,
    *,
    rc_errors=None,
    rc_url=None,
    rc_vdata=None,
    fuzzy_result=None,
):
    """
    Kör verify_citation med mockade beroenden.

    rc_errors   – lista av error/warning-dicts från checker.verify_reference
    rc_url      – URL-sträng från checker.verify_reference
    rc_vdata    – verified_data-dict från checker.verify_reference
    fuzzy_result – returvärde för _fuzzy_fallback (None = ingen träff)
    """
    mock_checker = MagicMock()
    mock_checker.verify_reference.return_value = (rc_errors or [], rc_url, rc_vdata)

    async def fake_get_checker():
        return mock_checker

    with (
        patch.object(srv, "_get_checker", fake_get_checker),
        patch.object(asyncio, "to_thread", _fake_to_thread),
        patch.object(srv, "_fuzzy_fallback", return_value=fuzzy_result),
    ):
        raw = asyncio.run(srv.verify_citation(
            title=title, authors=authors, year=year,
            doi=doi, arxiv_id=arxiv_id, url=url,
        ))

    return json.loads(raw)


VDATA_ATTENTION = {
    "title": "Attention Is All You Need",
    "authors": ["Ashish Vaswani", "Noam Shazeer"],
    "year": 2017,
    "venue": "NeurIPS",
}

UNVERIFIED_ERR = [{"error_type": "unverified", "error_details": "Paper not found"}]

FUZZY_MATCH = {
    "title": "Attention Is All You Need",
    "authors": ["Ashish Vaswani", "Noam Shazeer"],
    "year": 2017,
    "venue": "Advances in Neural Information Processing Systems",
    "url": "https://doi.org/10.48550/arXiv.1706.03762",
    "similarity": 92,
}


# ---------------------------------------------------------------------------
# Lyckade fall
# ---------------------------------------------------------------------------

def test_verified_exact_match():
    result = _call(
        "Attention Is All You Need",
        authors=["Ashish Vaswani", "Noam Shazeer"],
        year=2017,
        arxiv_id="1706.03762",
        rc_vdata=VDATA_ATTENTION,
    )
    assert result["verified"] is True
    assert result["errors"] is None
    assert result["matched_paper"]["title"] == "Attention Is All You Need"
    assert result["matched_paper"]["year"] == 2017


def test_verified_minor_author_variation():
    """Initialer vs fullnamn ska inte blockera verifiering."""
    result = _call(
        "Attention Is All You Need",
        authors=["A. Vaswani", "N. Shazeer"],
        year=2017,
        rc_vdata=VDATA_ATTENTION,
    )
    assert result["verified"] is True


# ---------------------------------------------------------------------------
# Mismatchar
# ---------------------------------------------------------------------------

def test_unverified_title_mismatch():
    result = _call(
        "Atention Is All You Need",
        rc_errors=UNVERIFIED_ERR,
        rc_vdata=None,
    )
    assert result["verified"] is False


def test_unverified_year_mismatch():
    """Year-mismatch-warning ska promotas till hard error."""
    result = _call(
        "Attention Is All You Need",
        year=2019,
        rc_errors=[{
            "warning_type": "year",
            "warning_details": "Year mismatch: cited 2019 vs actual 2017",
        }],
        rc_vdata=VDATA_ATTENTION,
    )
    assert result["verified"] is False


def test_not_found_returns_false():
    result = _call(
        "A Completely Made Up Paper Title That Does Not Exist",
        rc_errors=UNVERIFIED_ERR,
        rc_vdata=None,
    )
    assert result["verified"] is False
    assert result["matched_paper"] is None


# ---------------------------------------------------------------------------
# Fuzzy fallback
# ---------------------------------------------------------------------------

def test_fuzzy_fallback_triggered():
    result = _call(
        "Attention Is All You Need",
        rc_errors=UNVERIFIED_ERR,
        rc_vdata=None,
        fuzzy_result=FUZZY_MATCH,
    )
    assert result["verified"] is False  # unverified, men possible_match finns
    assert result["possible_match"] is not None
    assert result["possible_match"]["similarity"] == 92
    warnings = result["warnings"] or []
    assert any(w.get("warning_type") == "fuzzy_match" for w in warnings)


def test_fuzzy_fallback_miss():
    result = _call(
        "Complete Nonsense Title XYZZY",
        rc_errors=UNVERIFIED_ERR,
        rc_vdata=None,
        fuzzy_result=None,
    )
    assert result["verified"] is False
    assert result["possible_match"] is None


def test_fuzzy_fallback_threshold():
    """Crossref-svar med svag titelmatchning ska inte accepteras (< 85%)."""
    crossref_response = {
        "status": "ok",
        "message": {
            "items": [
                {
                    "title": ["All You Need Is Love"],
                    "author": [{"given": "John", "family": "Lennon"}],
                    "issued": {"date-parts": [[1967]]},
                    "URL": "https://doi.org/10.1234/fake",
                    "container-title": ["Beatles Journal"],
                }
            ]
        },
    }

    class FakeResponse:
        status_code = 200
        def json(self):
            return crossref_response

    with patch("requests.get", return_value=FakeResponse()):
        result = srv._fuzzy_fallback("Attention Is All You Need")

    assert result is None


# ---------------------------------------------------------------------------
# Regressionstester
# ---------------------------------------------------------------------------

def test_arxiv_id_float_coercion():
    """arxiv_id levererat som float ska coercas till str utan TypeError."""
    result = _call(
        "Attention Is All You Need",
        arxiv_id=1706.03762,  # float, som JSON-parser kan leverera
        rc_vdata=VDATA_ATTENTION,
    )
    assert result["verified"] is True


def test_verified_flag_hard_errors_only():
    """Saknade fält (missing) demoteras till warnings — ska inte blockera verified."""
    result = _call(
        "BERT: Pre-training of Deep Bidirectional Transformers",
        rc_errors=[{
            "error_type": "year",
            "error_details": "Year missing",
        }],
        rc_vdata={
            "title": "BERT: Pre-training of Deep Bidirectional Transformers",
            "authors": ["Jacob Devlin"],
            "year": 2019,
            "venue": "NAACL",
        },
    )
    assert result["verified"] is True


def test_raw_text_field_present():
    """_build_reference ska alltid inkludera raw_text-fältet."""
    ref = srv._build_reference(
        title="Attention Is All You Need",
        authors=["Ashish Vaswani"],
        year=2017,
        doi=None,
        arxiv_id=None,
        url=None,
    )
    assert "raw_text" in ref
    assert "Attention Is All You Need" in ref["raw_text"]


# ---------------------------------------------------------------------------
# Venue-varningar
# ---------------------------------------------------------------------------

def test_arxiv_preprint_warning():
    """arXiv-preprint-varning ska landa i warnings, inte blockera verified."""
    result = _call(
        "Attention Is All You Need",
        rc_errors=[{
            "warning_type": "venue",
            "warning_details": "arxiv preprint vs NeurIPS",
        }],
        rc_vdata=VDATA_ATTENTION,
    )
    assert result["verified"] is True
    warnings = result["warnings"] or []
    assert any(
        "arxiv preprint" in (w.get("warning_details") or "").lower()
        for w in warnings
    )


def test_published_venue_no_warning():
    """Korrekt paper utan fel ska ge verified=True och inga varningar."""
    result = _call(
        "Attention Is All You Need",
        rc_vdata=VDATA_ATTENTION,
    )
    assert result["verified"] is True
    assert result["warnings"] is None
