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
- [x] Skissat MCP-server med verktyget `verify_citation`.
- [x] Skapat paketstruktur `mcp_refchecker/` + `pyproject.toml`.
- [x] Skapat GitHub-repo: https://github.com/JonasBaath/mcp-refchecker (privat, tag v0.1.0 pre-release).
- [x] Betatest: 10/10 fall godkända i clean venv.
- [x] Registrera i `claude_desktop_config.json` och verifiera end-to-end via Claude Desktop (Fas 3).
  - Quirk: kräver ny konversation efter att connector lagts till — befintliga konversationer plockar inte upp verktyget.
- [ ] Skriv testsvit (`tests/`) — prioritet: regressionstest för float-coercion + fuzzy fallback-logik (se Testplan nedan).
- [ ] Sätt upp CI/CD — pytest-matrix 3.9/3.11/3.12 + publish-workflow vid `v*`-release-tags (se CI/CD nedan).
- [ ] Göra GitHub-repot publikt.
- [ ] PyPI-publicering (v0.2?) — CI/CD-publish-workflow ska vara på plats först.

## Designbeslut (implementerat)
- **Singleton-checker + `asyncio.Lock`** — `ArxivReferenceChecker.__init__` är dyrt, initieras lat vid första anropet
- **`asyncio.to_thread(checker.verify_reference, ...)`** — synkrona HTTP-anrop måste av async-loopen
- **`llm_config={"disabled": True}`** — bara källverifiering, ingen LLM-hallucinationsdetektion
- **`types.SimpleNamespace`-stub** för `source_paper` — används bara för loggning i refchecker
- **`raw_text`-fält** byggs från title+authors+year — krävs för att undvika `KeyError` i `verify_reference_standard`
- **Normalisering av refchecker-output**:
  - Promotera plain `year`/`author`/`venue` warnings med "mismatch" till hårda fel (refchecker är inkonsekvent — markerar year-mismatch som warning men author-mismatch som error)
  - Demotera `error_type` med "missing" till warnings när paper hittades (saknade input-fält är inte hallucination)
  - Version-relaterade warnings (`(v6 vs v7)`) och arxiv-preprint-vs-venue lämnas som warnings
- **Fuzzy fallback mot Crossref** när refchecker returnerar unverified — fångar stilistiska variationer men INTE riktiga stavfel (se README)
- **String-tvingning av `doi`/`arxiv_id`** — JSON-parsers kan leverera arXiv-IDn som float (`1706.03762`); coercas till `str` tidigt i `verify_citation` via `str(doi)` resp. `str(arxiv_id)`

## Fuzzy fallback-upptäckten
Provade i ordning: **Semantic Scholar** (rate-limit 429 utan API-nyckel) → **OpenAlex** (ingen fuzzy-matchning alls — strikt token-sökning, noll träffar på typos) → **Crossref** (bäst av de tre, men samma fundamentala begränsning).

**Slutsats:** Ingen fri akademisk API klarar riktiga stavfel i titlar. Alla gör keyword/token-matchning och ett felstavat ord försvinner ur indexet. För att lösa det skulle semantic embeddings krävas (OpenAI/Voyage — paid API). Begränsningen är dokumenterad i README under "Fuzzy fallback and its limitations".

## Designbeslut
- **Singleton-checker + `asyncio.Lock`** — `__init__` är dyr (bygger hybrid-checker, ev. LLM, web search). Initieras lat vid första anropet.
- **`asyncio.to_thread(checker.verify_reference, ...)`** — `verify_reference` är synkron och gör HTTP-anrop; måste av async-loopen.
- **`llm_config={"disabled": True}`** — vi vill ha källverifiering, inte LLM-hallucinationsdetektion (kräver egen API-nyckel och är ortogonal mot syftet).
- **Mock `SimpleNamespace` istället för `_create_local_file_paper()`** — `source_paper` används bara för loggning/rapportering i `verify_reference`-vägen. En stub räcker och slipper filsystemberoenden.
- **Auto-fyll `reference['url']` från DOI/arXiv-ID** — flera underliggande checkers (CrossRef, ArXivCitationChecker) extraherar IDs ur `url`-fältet, så vi sätter det om det saknas.
- **Verktygsschema:** `title` (krav), `authors`, `year`, `doi`, `arxiv_id`, `url`. Returnerar JSON med `verified`, `url`, `matched_paper` (normaliserad), och `errors`.

## Verktyg
`verify_citation` — tar titel, författare, år och eventuell DOI/arXiv-ID/URL och returnerar verifieringsresultat (matchat paper + ev. fellista).

## Testplan

**Framework:** pytest + unittest.mock + pytest-cov
**Kör:** `pytest tests/ --cov=. --cov-report=term-missing`
**Installera:** `pip install -e ".[dev]"` (pytest + pytest-cov under `[project.optional-dependencies] dev` i pyproject.toml)

Fixtures i `tests/fixtures/` — JSON-filer med mockade API-svar (Crossref, Semantic Scholar). Mocka HTTP-anrop med `unittest.mock.patch("requests.get")` — testerna ska INTE göra verkliga nätverksanrop.

### Testkategorier

**Lyckade fall**
- `test_verified_exact_match` — alla fält stämmer → `verified: true`, inga varningar
- `test_verified_minor_author_variation` — initialer vs fullnamn → `verified: true`, möjlig warning

**Mismatchar**
- `test_unverified_title_mismatch` — paper hittas, titel skiljer sig markant → `verified: false`
- `test_unverified_year_mismatch` — rätt paper, fel år → `verified: false`
- `test_not_found_returns_false` — inget hittas → `verified: false`

**Fuzzy fallback (prioritet 1 — komplex logik med många commits)**
- `test_fuzzy_fallback_triggered` — mockad Crossref hittar paper → fallback-markering i output
- `test_fuzzy_fallback_miss` — varken S2 eller Crossref → `verified: false`
- `test_fuzzy_fallback_threshold` — svag titelmatchning → ska EJ accepteras

**Regressionstester**
- `test_arxiv_id_float_coercion` — arxiv_id in som float → coercas till str, inget TypeError
- `test_verified_flag_hard_errors_only` — saknade fält är warnings, blockerar ej verified
- `test_raw_text_field_present` — output-JSON innehåller `raw_text`-fält

**Venue-varningar**
- `test_arxiv_preprint_warning` — venue är arxiv.org-URL → preprint-warning inkluderas
- `test_published_venue_no_warning` — venue är tidskriftsnamn → ingen preprint-varning

## CI/CD

Två workflow-filer:

**`.github/workflows/ci.yml`** — triggas vid push + pull_request:
```yaml
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ['3.9', '3.11', '3.12']
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python-version }}" }
      - run: pip install -e ".[dev]"
      - run: pytest tests/ --cov=. --cov-report=term-missing
```

**`.github/workflows/publish.yml`** — triggas vid release-tags `v*`:
```yaml
jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.11' }
      - run: pip install build twine
      - run: python -m build
      - run: twine upload dist/* --skip-existing
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}
```

**Förkrav:**
- `pytest` + `pytest-cov` under `[project.optional-dependencies] dev` i pyproject.toml
- `PYPI_TOKEN` som GitHub Secret (Settings → Secrets → Actions) — genereras på pypi.org
- Publish triggas BARA på release-tags (`v0.2.0` etc.), inte vid varje push
- `--skip-existing` förhindrar fel om taggen pushas två gånger

**Dependabot:** `.github/dependabot.yml` bevakar `academic-refchecker` med veckovis pip-check.

## Licens
MIT (refchecker). MCP-wrappern: MIT.
