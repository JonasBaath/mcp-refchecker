# mcp-refchecker

An MCP server that lets Claude verify academic citations in real time against [Semantic Scholar](https://www.semanticscholar.org/), [OpenAlex](https://openalex.org/), and [Crossref](https://www.crossref.org/) — catching hallucinated or incorrect references before they end up in your work.

Built on top of [academic-refchecker](https://github.com/markrussinovich/refchecker) (MIT).

## Tool

**`verify_citation`** — verifies that a cited paper exists and that its metadata (title, authors, year, venue) matches what was cited.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `title` | string | yes | Title of the cited paper |
| `authors` | string[] | no | List of author names |
| `year` | integer | no | Publication year |
| `doi` | string | no | DOI (e.g. `10.1145/12345`) |
| `arxiv_id` | string | no | arXiv ID (e.g. `2301.00001`) |
| `url` | string | no | Direct URL to the paper |

Returns JSON:
```json
{
  "verified": true,
  "url": "https://...",
  "matched_paper": {
    "title": "...",
    "authors": [...],
    "year": 2023,
    "venue": "..."
  },
  "possible_match": null,
  "errors": null,
  "warnings": null,
  "info": null
}
```

### Result fields

- **`verified`** — `true` if the paper was found and all provided metadata (year, authors, venue) matches. `false` if there is a real metadata conflict or the paper could not be found.
- **`matched_paper`** — the authoritative metadata from the verification source.
- **`possible_match`** — a Crossref fallback match when the exact title was not found but a close variant was (see "Fuzzy fallback" below).
- **`errors`** — hard errors that block verification (wrong year, wrong authors, paper not found).
- **`warnings`** — soft warnings that don't block verification (arXiv v1 vs v2 differences, arXiv preprint vs published venue, incomplete input metadata).
- **`info`** — informational suggestions (e.g., "reference could include arXiv URL").

### What counts as an error vs a warning

`academic-refchecker` returns a flat list of issues with some inconsistency (year mismatches get marked as warnings while author mismatches get marked as errors). This wrapper normalises the output:

- **Promoted to hard errors:** plain `year`/`author`/`venue` mismatches where the cited metadata actually differs from reality. These block `verified`.
- **Demoted to warnings:** "missing field" errors when the paper was found but the user didn't provide that field in the first place. Missing input metadata is not evidence of a hallucinated citation.
- **Kept as warnings:** arXiv version differences (v1 vs v2), preprint-vs-published venue notes.

### Fuzzy fallback and its limitations

When `academic-refchecker` reports that a paper could not be verified, this wrapper makes a secondary query to [Crossref](https://api.crossref.org/) using fuzzy title matching and `fuzzywuzzy.ratio`. If a candidate with ≥ 85% similarity is found, it's returned as `possible_match` with a warning.

**What the fuzzy fallback catches:**
- Stylistic title variations (case differences, punctuation, word order)
- Minor rewording
- Titles where refchecker's strict comparison rejected an otherwise valid match

**What the fuzzy fallback does NOT catch:**
- Real typos in distinctive title words (e.g., "Atention Is All You Need")
- Heavily mangled titles

This is a fundamental limitation of free academic search APIs. Crossref, OpenAlex, and Semantic Scholar all do keyword/token-based search — as soon as a distinctive word is misspelled, it simply isn't in the search index, and the real paper won't appear in results regardless of how you post-process them. Catching real typos would require semantic embeddings from a paid API (OpenAI, Voyage, etc.) or a full-text fuzzy search engine, neither of which is exposed by free scholarly data sources.

If you suspect a typo but `verify_citation` returns unverified, the best workaround is to rewrite the title in the most canonical form you can and try again.

## Installation

```bash
pip install mcp-refchecker
```

Or from source:

```bash
git clone https://github.com/JonasBaath/mcp-refchecker
cd mcp-refchecker
pip install .
```

## Configuration

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "refchecker": {
      "command": "mcp-refchecker"
    }
  }
}
```

### Optional environment variables

- **`SEMANTIC_SCHOLAR_API_KEY`** — [apply for one here](https://www.semanticscholar.org/product/api) for higher rate limits on refchecker's primary verification path.
- **`CROSSREF_MAILTO`** — your contact email, used to opt into Crossref's [polite pool](https://api.crossref.org/swagger-ui/index.html) for more reliable fuzzy fallback access.
- **`MCP_REFCHECKER_DEBUG`** — set to any non-empty value to print debug logging from the fuzzy fallback path to stderr.

Example with all optional settings:

```json
{
  "mcpServers": {
    "refchecker": {
      "command": "mcp-refchecker",
      "env": {
        "SEMANTIC_SCHOLAR_API_KEY": "your-key-here",
        "CROSSREF_MAILTO": "you@example.com"
      }
    }
  }
}
```

## License

MIT — © Jonas Bååth. Built on [academic-refchecker](https://github.com/markrussinovich/refchecker) (MIT).
