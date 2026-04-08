# MCP-Refchecker

## Syfte
Bygga en MCP-server som låter Claude verifiera akademiska citeringar i realtid mot Semantic Scholar, OpenAlex och CrossRef, för att undvika hallucineringar.

## Plan
Wrappa `academic-refchecker`-paketet som en MCP-server genom att importera Python-paketet direkt (inte CLI eller REST-API).

Kärn-API:et i paketet:
- `ArxivReferenceChecker(semantic_scholar_api_key, llm_config, ...)` — initiering. Bygger internt `EnhancedHybridReferenceChecker` med ordning **Semantic Scholar API → OpenAlex → CrossRef** när inget `db_path` anges.
- `verify_reference(source_paper, reference)` → `(errors, url, verified_data)` — verifiering av enskild referens. Synkron, nätverksbunden. Routar via `verify_reference_standard()` och har inbyggd ArXiv-re-verifiering vid katastrofal författarmismatch samt URL-fallback.
- `source_paper` behöver inte vara en riktig artikel — en `types.SimpleNamespace`-stub med `title`, `authors`, `published.year`, `canonical_url`, `external_paper_id`, `venue` räcker. (`_create_local_file_paper()` finns men är överkurs för MCP-fallet.)

## Status
- [x] Granskat `ArxivReferenceChecker.__init__` (`refchecker.py:263`) och `verify_reference()` (`refchecker.py:2588`).
- [x] Skissat MCP-server (~130 rader) med verktyget `verify_citation`.
- [ ] Skapa paketstruktur `mcp_refchecker/` + `pyproject.toml`.
- [ ] Testa mot riktiga referenser (känd korrekt + känd hallucinerad).
- [ ] Registrera i `claude_desktop_config.json` och verifiera end-to-end via Claude Desktop.

## Designbeslut
- **Singleton-checker + `asyncio.Lock`** — `__init__` är dyr (bygger hybrid-checker, ev. LLM, web search). Initieras lat vid första anropet.
- **`asyncio.to_thread(checker.verify_reference, ...)`** — `verify_reference` är synkron och gör HTTP-anrop; måste av async-loopen.
- **`llm_config={"disabled": True}`** — vi vill ha källverifiering, inte LLM-hallucinationsdetektion (kräver egen API-nyckel och är ortogonal mot syftet).
- **Mock `SimpleNamespace` istället för `_create_local_file_paper()`** — `source_paper` används bara för loggning/rapportering i `verify_reference`-vägen. En stub räcker och slipper filsystemberoenden.
- **Auto-fyll `reference['url']` från DOI/arXiv-ID** — flera underliggande checkers (CrossRef, ArXivCitationChecker) extraherar IDs ur `url`-fältet, så vi sätter det om det saknas.
- **Verktygsschema:** `title` (krav), `authors`, `year`, `doi`, `arxiv_id`, `url`. Returnerar JSON med `verified`, `url`, `matched_paper` (normaliserad), och `errors`.

## Verktyg
`verify_citation` — tar titel, författare, år och eventuell DOI/arXiv-ID/URL och returnerar verifieringsresultat (matchat paper + ev. fellista).

## Licens
MIT (refchecker). MCP-wrappern: MIT.
