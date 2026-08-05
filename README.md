# arxiv-mcpserver

An MCP (Model Context Protocol) server that provides tools for searching and retrieving academic papers from arXiv.org.

## What it does

This server gives AI agents access to arXiv through 8 specialized tools:

- **arxiv_search** - General keyword search
- **arxiv_search_advanced** - Advanced queries with Boolean operators and field prefixes
- **arxiv_search_by_author** - Find papers by specific author
- **arxiv_search_by_category** - Browse papers by subject category
- **arxiv_get_paper** - Retrieve papers by arXiv ID
- **arxiv_get_latest** - Get newest papers in a category
- **arxiv_get_pdf_url** - Get direct PDF download link
- **arxiv_list_categories** - Browse the complete arXiv subject taxonomy

All tools support dual output formats (JSON for machines, Markdown for humans) and include proper input validation, rate limiting, and caching.

## Installation

```bash
# Clone the repository
git clone https://github.com/rivaldofwijaya/arxiv-mcpserver.git
cd arxiv-mcpserver

# Install dependencies
pip install -r requirements.txt
```

Or with a virtual environment:

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Usage with AI Agents

### Claude Desktop

Add this to your Claude Desktop config file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "arxiv-mcpserver": {
      "command": "python",
      "args": ["/absolute/path/to/arxiv-mcpserver/server.py"]
    }
  }
}
```

### Claude Code / OpenCode / OpenClaw

Add the same configuration to your MCP settings:

```json
{
  "mcpServers": {
    "arxiv-mcpserver": {
      "command": "python",
      "args": ["/absolute/path/to/arxiv-mcpserver/server.py"]
    }
  }
}
```

### Codex

Configure your Codex MCP settings to include the arXiv server:

```json
{
  "mcpServers": {
    "arxiv-mcpserver": {
      "command": "python",
      "args": ["/path/to/arxiv-mcpserver/server.py"]
    }
  }
}
```

### Testing with MCP Inspector

For development and testing, you can use the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector python server.py
```

## Features

- **Rate Limiting**: Automatically enforces arXiv's 3-second request interval
- **Caching**: 24-hour cache to reduce redundant API calls
- **Dual Formats**: JSON (complete data) and Markdown (human-readable)
- **Validation**: Pydantic models validate all inputs
- **Error Handling**: Clear, actionable error messages

## Example Usage

Once connected to your AI agent, you can:

```
Search for "transformer attention mechanism" papers
Find papers by Geoffrey Hinton
Get the latest papers in cs.LG
Retrieve paper 1706.03762 with full metadata
List all available arXiv categories
```

## Configuration

Optional environment variables:

```bash
export ARXIV_RATE_LIMIT_DELAY=3.0    # Override rate limit (seconds)
export ARXIV_CACHE_EXPIRY=86400      # Override cache duration (seconds)
```

## Available Tools

### 1. arxiv_search
General search across all arXiv fields.

```json
{"query": "neural networks", "max_results": 10}
```

### 2. arxiv_search_advanced
Use Boolean operators and field prefixes.

```json
{
  "query": "au:\"Geoffrey Hinton\" AND cat:stat.ML",
  "max_results": 20
}
```

Supported prefixes: `ti` (title), `au` (author), `abs` (abstract), `cat` (category), `all` (allfields)
Boolean operators: `AND`, `OR`, `ANDNOT`

### 3. arxiv_search_by_author
Find papers by author with optional filters.

```json
{
  "author_name": "Geoffrey Hinton",
  "category": "stat.ML",
  "start_date": "202001010000",
  "end_date": "202212312359",
  "max_results": 15
}
```

### 4. arxiv_search_by_category
Browse papers in a category.

```json
{"category": "cs.AI", "max_results": 20}
```

### 5. arxiv_get_paper
Retrieve papers by arXiv ID.

```json
{"id_list": ["1706.03762", "1810.04805v1"]}
```

### 6. arxiv_get_latest
Get newest papers in a category.

```json
{"category": "cs.LG", "max_results": 10}
```

### 7. arxiv_get_pdf_url
Get PDF download link.

```json
{"paper_id": "1706.03762"}
```

### 8. arxiv_list_categories
Get complete arXiv taxonomy.

```json
{}
```

## Pagination

For many results, use pagination:

```json
// First page
{"query": "machine learning", "start": 0, "max_results": 10}

// Second page
{"query": "machine learning", "start": 10, "max_results": 10}
```

## Date Filtering

Use format `YYYYMMDDHHMM` (24-hour time, GMT):

```json
{
  "query": "quantum computing",
  "start_date": "202301010000",
  "end_date": "202312312359"
}
```

## Response Formats

All tools support either `json` (complete data) or `markdown` (human-readable).

```json
{"query": "transformer", "response_format": "json"}
{"query": "transformer", "response_format": "markdown"}
```

## Output Fields

Each paper includes: ID, title, summary/abstract, authors, published date, updated date, primary category, all categories, journal reference (if published), DOI (if available), and URLs for abstract and PDF pages.

## Testing

Run the test suite:

```bash
python scripts/test_server_local.py
```

## Rate Limiting & Caching

- **Rate Limiting**: Enforces arXiv's 3-second delay between requests
- **Caching**: 24-hour cache reduces redundant API calls
- **Compliance**: Uses HTTPS endpoint and respects arXiv Terms of Use

## Project Structure

```
arxiv-mcpserver/
├── server.py                      # Main MCP server
├── requirements.txt               # Dependencies
├── LICENSE                        # MIT License
├── pyproject.toml                 # Project metadata
├── README.md                      # This file
└── scripts/
    ├── test_server_local.py       # Test suite
    ├── evaluation.py              # Evaluation harness
    ├── connections.py             # MCP utilities
    ├── requirements.txt           # Script dependencies
    └── example_evaluation.xml     # Example tests
```

## Dependencies

Core: `mcp[cli]`, `httpx`, `pydantic`, `feedparser`

For testing: `anthropic` (see `scripts/requirements.txt`)

## Known Issues

**Author name matching**: Results may vary depending on name format. Try variations:
- Full name: "Geoffrey Hinton"
- With initials: "G. Hinton"
- Last name only: "Hinton"

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/name`)
3. Commit changes (`git commit -m 'Add feature'`)
4. Push to branch (`git push origin feature/name`)
5. Open a Pull Request

## License

MIT License - see [LICENSE](LICENSE) for details.

When using this server with arXiv data, also comply with:
- arXiv API Terms of Use
- CC BY-SA 4.0 License (for arXiv content)

## Resources

- [arXiv API Documentation](https://info.arxiv.org/help/api/user-manual.html)
- [MCP Specification](https://modelcontextprotocol.io/)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

---

**Created by [rivaldofwijaya](https://github.com/rivaldofwijaya)**