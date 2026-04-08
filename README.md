# mcp-refchecker

An MCP server that lets Claude verify academic citations in real time against [Semantic Scholar](https://www.semanticscholar.org/), [OpenAlex](https://openalex.org/), and [CrossRef](https://www.crossref.org/) — catching hallucinated or incorrect references before they end up in your work.

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
  "errors": null
}
```

`errors` is `null` if the citation checks out, or a list of error objects describing mismatches if not.

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

Optional: set a [Semantic Scholar API key](https://www.semanticscholar.org/product/api) for higher rate limits:

```json
{
  "mcpServers": {
    "refchecker": {
      "command": "mcp-refchecker",
      "env": {
        "SEMANTIC_SCHOLAR_API_KEY": "your-key-here"
      }
    }
  }
}
```

## License

MIT — © Jonas Bååth. Built on [academic-refchecker](https://github.com/markrussinovich/refchecker) (MIT).
