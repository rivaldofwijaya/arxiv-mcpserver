#!/usr/bin/env python3
'''
MCP Server for the arXiv API.

This server provides tools to search, explore, and retrieve academic papers
from the arXiv API. It follows MCP best practices and implements read-only
tools to gather data seamlessly.
'''

import asyncio
import json
import logging
import time
import urllib.parse
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any, Tuple

import feedparser
import httpx
from pydantic import BaseModel, Field, ConfigDict, field_validator
from mcp.server.fastmcp import FastMCP, Context

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("arxiv-mcpserver")

def _log_debug(msg: str):
    logger.debug(msg)

# Initialize the MCP server
mcp = FastMCP("arxiv-mcpserver")

# Constants
API_BASE_URL = "https://export.arxiv.org/api/query"
RATE_LIMIT_DELAY = 3.0  # Seconds between requests as per Arxiv guidelines
CACHE_EXPIRY = 24 * 60 * 60  # 24 hours in seconds
MAX_CACHE_ENTRIES = 1000  # Maximum number of cached queries

class ArxivCache:
    '''Simple in-memory cache for Arxiv responses with size limiting.'''
    def __init__(self):
        self._cache: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self._cache:
            timestamp, data = self._cache[key]
            if time.time() - timestamp < CACHE_EXPIRY:
                return data
            else:
                del self._cache[key]
        return None

    def set(self, key: str, data: Any):
        # Enforce cache size limit by removing oldest entries
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            # Remove oldest 10% of cache entries
            entries_to_remove = max(1, MAX_CACHE_ENTRIES // 10)
            oldest_keys = sorted(self._cache.keys(), 
                               key=lambda k: self._cache[k][0])[:entries_to_remove]
            for old_key in oldest_keys:
                del self._cache[old_key]
            logger.debug(f"Cache limit reached, removed {entries_to_remove} old entries")
        
        self._cache[key] = (time.time(), data)

cache = ArxivCache()

class RateLimiter:
    '''Ensures at least RATE_LIMIT_DELAY seconds between calls.'''
    def __init__(self):
        self.last_call_time = 0.0
        self.lock = asyncio.Lock()

    async def wait(self):
        async with self.lock:
            elapsed = time.time() - self.last_call_time
            if elapsed < RATE_LIMIT_DELAY:
                wait_time = RATE_LIMIT_DELAY - elapsed
                logger.debug(f"Rate limiting: waiting {wait_time:.2f}s")
                await asyncio.sleep(wait_time)
            self.last_call_time = time.time()

limiter = RateLimiter()

# Formatting Enums
class ResponseFormat(str, Enum):
    '''Output format for tool responses.'''
    MARKDOWN = "markdown"
    JSON = "json"

class SortBy(str, Enum):
    '''Allowed sort by criteria.'''
    RELEVANCE = "relevance"
    LAST_UPDATED_DATE = "lastUpdatedDate"
    SUBMITTED_DATE = "submittedDate"

class SortOrder(str, Enum):
    '''Allowed sort order criteria.'''
    ASCENDING = "ascending"
    DESCENDING = "descending"

# Input Models
class ArxivSearchInput(BaseModel):
    '''Input model for generic arXiv search.'''
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    query: str = Field(..., description="""The query string. e.g., 'all:electron', 'ti:"exact phrase"', 'au:del_maestro AND ti:checkerboard', 'au:"Geoffrey Hinton" AND cat:stat.ML'. Use quotes for exact phrases to avoid special character issues.""")
    start_date: Optional[str] = Field(default=None, description="Optional start date in format YYYYMMDDHHMM (24-hour time, GMT)")
    end_date: Optional[str] = Field(default=None, description="Optional end date in format YYYYMMDDHHMM (24-hour time, GMT). Only valid if start_date is also provided.")
    start: Optional[int] = Field(default=0, description="Number of results to skip for pagination (0-based init). Increment this to page through results.", ge=0)
    max_results: Optional[int] = Field(default=10, description="Max results to return (max 2000 per request, keep low for agents)", ge=1, le=2000)
    sort_by: Optional[SortBy] = Field(default=SortBy.RELEVANCE, description="Sorting parameter")
    sort_order: Optional[SortOrder] = Field(default=SortOrder.DESCENDING, description="Sorting order")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")

    @field_validator('query')
    @classmethod
    def validate_query(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Query cannot be empty")
        return v.strip()

    @field_validator('start_date', 'end_date')
    @classmethod
    def validate_dates(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.isdigit() or len(v) != 12:
             raise ValueError("Date must be in YYYYMMDDHHMM format (12 digits)")
        try:
             import datetime
             datetime.datetime.strptime(v, "%Y%m%d%H%M")
        except ValueError:
             raise ValueError("Invalid date or time components")
        return v

class ArxivGetPaperInput(BaseModel):
    '''Input model to retrieve specific arXiv papers by ID.'''
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    id_list: List[str] = Field(..., description="List of arXiv IDs (e.g., '0710.5765v1', 'hep-ex/0307015')", min_length=1, max_length=50)
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")

class ArxivAuthorSearchInput(BaseModel):
    '''Input model for searching papers by author name, with optional category filtering.'''
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    author_name: str = Field(..., description="Name of the author to search for. Consider using just the last name or initials if exact match fails (e.g. 'R. P. Feynman' vs 'Richard Feynman').")
    category: Optional[str] = Field(default=None, description="Optional category to filter by (e.g. 'stat.ML', 'cs.LG', 'cs.AI'). When provided, only papers in this category are returned.")
    start_date: Optional[str] = Field(default=None, description="Optional start date in format YYYYMMDDHHMM (24-hour time, GMT)")
    end_date: Optional[str] = Field(default=None, description="Optional end date in format YYYYMMDDHHMM (24-hour time, GMT). Only valid if start_date is also provided.")
    start: Optional[int] = Field(default=0, description="Number of results to skip for pagination", ge=0)
    max_results: Optional[int] = Field(default=10, description="Max results to return", ge=1, le=100)
    sort_by: Optional[SortBy] = Field(default=SortBy.SUBMITTED_DATE, description="Sorting parameter")
    sort_order: Optional[SortOrder] = Field(default=SortOrder.DESCENDING, description="Sorting order")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")

    @field_validator('start_date', 'end_date')
    @classmethod
    def validate_dates(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.isdigit() or len(v) != 12:
             raise ValueError("Date must be in YYYYMMDDHHMM format (12 digits)")
        try:
             import datetime
             datetime.datetime.strptime(v, "%Y%m%d%H%M")
        except ValueError:
             raise ValueError("Invalid date or time components")
        return v

class ArxivCategorySearchInput(BaseModel):
    '''Input model for browsing a category, with optional author filtering.'''
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    category: str = Field(..., description="The category ID to browse (e.g., 'cs.AI', 'physics.optics', 'stat.ML')")
    author_name: Optional[str] = Field(default=None, description="Optional author name to filter by. When provided, only papers by this author in the category are returned.")
    start_date: Optional[str] = Field(default=None, description="Optional start date in format YYYYMMDDHHMM (24-hour time, GMT)")
    end_date: Optional[str] = Field(default=None, description="Optional end date in format YYYYMMDDHHMM (24-hour time, GMT). Only valid if start_date is also provided.")
    start: Optional[int] = Field(default=0, description="Number of results to skip for pagination", ge=0)
    max_results: Optional[int] = Field(default=10, description="Max results to return", ge=1, le=100)
    sort_by: Optional[SortBy] = Field(default=SortBy.SUBMITTED_DATE, description="Sorting parameter")
    sort_order: Optional[SortOrder] = Field(default=SortOrder.DESCENDING, description="Sorting order")
    response_format: ResponseFormat = Field(default=ResponseFormat.MARKDOWN, description="Output format")

    @field_validator('start_date', 'end_date')
    @classmethod
    def validate_dates(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.isdigit() or len(v) != 12:
             raise ValueError("Date must be in YYYYMMDDHHMM format (12 digits)")
        try:
             datetime.strptime(v, "%Y%m%d%H%M")
        except ValueError:
             raise ValueError("Invalid date or time components")
        return v

class ArxivGetPdfUrlInput(BaseModel):
    '''Input model for getting the PDF download URL of an arXiv paper.'''
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    paper_id: str = Field(..., description="The arXiv ID (e.g., '2105.14321')")


# Helper Functions
async def _make_api_request(params: dict) -> dict:
    '''Reusable function for arXiv API calls with rate limiting and caching.
    
    URL Encoding Note:
    The 'safe' parameter in urlencode preserves arXiv query syntax characters:
    - ':' separates field prefixes from values (e.g., 'au:Hinton')
    - '+' represents spaces in query AND is part of date range syntax (e.g., '[start+TO+end]')
    - '[]()' used for grouping and Boolean expressions
    
    These characters must NOT be encoded to maintain arXiv's query language semantics.
    Date parameters in validated models are guaranteed to be 12 digits (numeric only),
    so they don't require additional encoding beyond the safe parameter handling.
    '''
    # Generate cache key from sorted params
    cache_key = json.dumps(params, sort_keys=True)
    cached_data = cache.get(cache_key)
    if cached_data:
        _log_debug("Returning cached results")
        return cached_data

    # Ensure rate limiting
    await limiter.wait()
    
    async with httpx.AsyncClient() as client:
        # Build query string - safe chars preserve arXiv query syntax
        query_string = urllib.parse.urlencode(params, safe=':+[]()')
        url = f"{API_BASE_URL}?{query_string}"
        
        logger.info(f"Fetching: {url}")
        try:
            response = await client.get(url, timeout=30.0)
            response.raise_for_status()
            
            # arXiv returns Atom XML. feedparser handles it reliably.
            feed = feedparser.parse(response.text)
            
            # Check for API-level errors in the feed (Arxiv wraps errors in Atom)
            if not feed.entries and 'Error' in feed.feed.get('title', ''):
                 error_msg = feed.feed.get('summary', 'Unknown Arxiv API Error')
                 raise Exception(f"Arxiv API Error: {error_msg}")

            # Cache the result
            cache.set(cache_key, feed)
            return feed
        except Exception as e:
            logger.error(f"API request failed: {str(e)}")
            raise

def _handle_api_error(e: Exception) -> str:
    '''Consistent error formatting across all tools with actionable suggestions.'''
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 400:
            return ("Error: Bad request (HTTP 400). Your query syntax may be invalid.\n"
                    "Tips: Ensure date format is YYYYMMDDHHMM (12 digits). "
                    "Check for unmatched quotes or invalid field prefixes. "
                    "Use `cat:` for categories, `au:` for authors, `ti:` for titles.")
        elif status == 403:
            return ("Error: Forbidden (HTTP 403). arXiv may be blocking this request.\n"
                    "This can happen with VPN/proxy IPs or excessive requests. Try again later.")
        elif status == 429:
            return ("Error: Rate limited (HTTP 429). arXiv requires ~3s between requests.\n"
                    "Wait a moment and retry. The server enforces rate limiting automatically.")
        elif status == 503:
            return ("Error: arXiv service unavailable (HTTP 503).\n"
                    "arXiv may be under maintenance. Check https://arxiv.org/status and retry later.")
        return f"Error: API request failed with HTTP {status}. Response: {e.response.text[:200]}"
    elif isinstance(e, httpx.TimeoutException):
        return ("Error: Request timed out. The arXiv server may be slow or your query is too broad.\n"
                "Try reducing max_results or using a more specific query.")

    error_str = str(e)
    if "Arxiv API Error" in error_str:
        return error_str
    return f"Error: {type(e).__name__} - {error_str}"

def _extract_paper_data(entry: Any) -> Dict[str, Any]:
    '''Extract relevant data from a feedparser entry into a structured dict.'''
    authors_data = []
    for author in entry.get('authors', []):
        name = author.get('name', '')
        # Arxiv uses custom namespace for affiliation
        affiliation = author.get('arxiv_affiliation', '')
        authors_data.append({"name": name, "affiliation": affiliation})
    
    categories = [tag.get('term', '') for tag in entry.get('tags', [])]
    primary_category = entry.get('arxiv_primary_category', {}).get('term', '')
    
    links = entry.get('links', [])
    pdf_url = next((link['href'] for link in links if link.get('title') == 'pdf'), None)
    abs_url = next((link['href'] for link in links if link.get('rel') == 'alternate'), entry.get('id'))

    raw_id = entry.get('id', '').split('/abs/')[-1]
    
    return {
        "id": raw_id,
        "title": entry.get('title', '').replace('\n', ' ').strip(),
        "summary": entry.get('summary', '').replace('\n', ' ').strip(),
        "authors": [a['name'] for a in authors_data],
        "authors_detailed": authors_data,
        "published": entry.get('published', ''),
        "updated": entry.get('updated', ''),
        "primary_category": primary_category,
        "categories": categories,
        "comment": entry.get('arxiv_comment', ''),
        "journal_ref": entry.get('arxiv_journal_ref', ''),
        "doi": entry.get('arxiv_doi', ''),
        "abs_url": abs_url,
        "pdf_url": pdf_url
    }

def _format_papers_markdown(papers: List[Dict[str, Any]], query_info: str, total_results: int, start: int, max_results: int) -> str:
    '''Formats a list of papers to an AI-friendly Markdown format.'''
    if not papers:
         return f"# arXiv Search Results\n\nNo papers found for **{query_info}**. Suggestions:\n- Use fewer keywords or remove special characters\n- Try a partial match or broader search\n- Use field prefixes: `au:` (author), `ti:` (title), `cat:` (category)\n- Check spelling of author names (try last name only)"

    lines = [
        f"# arXiv Search Results: {query_info}",
        f"Showing results {start + 1} to {start + len(papers)} of approximately **{total_results}** total."
    ]
    if total_results > 1000:
        lines.append("> **Tip:** Many results returned. Narrow your search using `ti:` (title), `au:` (author), or `cat:` (category) prefixes.")
    elif total_results == 0:
        lines.append("> No results found. Try broader search terms or different keywords.")

    lines.append("---")
    lines.append("")

    for paper in papers:
        lines.append(f"### {paper['title']}")
        lines.append(f"**arXiv ID:** `{paper['id']}` | **Published:** {paper['published'][:10]} | **Primary Category:** `{paper['primary_category']}`")
        if paper['categories'] and len(paper['categories']) > 1:
            cross_listed = [c for c in paper['categories'] if c != paper['primary_category']]
            if cross_listed:
                lines.append(f"**Cross-List Categories:** `{', '.join(cross_listed)}`")

        # Format authors with affiliations if available
        authors_fmt = []
        for auth in paper['authors_detailed']:
            if auth['affiliation']:
                authors_fmt.append(f"{auth['name']} (*{auth['affiliation']}*)")
            else:
                authors_fmt.append(auth['name'])
        lines.append(f"**Authors:** {', '.join(authors_fmt)}")

        abstract = paper['summary']
        if len(abstract) > 600:
             abstract = abstract[:597] + "..."
             lines.append(f"\n> **Abstract:** {abstract}")
             lines.append(f"\n> ⚠️ *Abstract truncated for readability. View full abstract at the link below.*\n")
        else:
             lines.append(f"\n> **Abstract:** {abstract}\n")

        links = [f"[Abstract]({paper['abs_url']})"]
        if paper['pdf_url']:
            links.append(f"[PDF]({paper['pdf_url']})")
        if paper['doi']:
            links.append(f"[DOI: {paper['doi']}](https://doi.org/{paper['doi']})")

        lines.append(" | ".join(links))

        if paper['comment']:
             lines.append(f"*Note: {paper['comment']}*")

        lines.append("\n---")

    return "\n".join(lines)


# --- TOOLS ---

@mcp.tool(
    name="arxiv_search_advanced",
    annotations={
        "title": "Advanced arXiv Search",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def arxiv_search_advanced(params: ArxivSearchInput) -> str:
    '''Execute an advanced search using arXiv's query syntax. USE THIS when you need to combine multiple filters (author + category, author + title, etc.).

    Supports field prefixes: ti (title), au (author), abs (abstract), cat (category), all (all fields).
    Supports boolean operators: AND, OR, ANDNOT.
    Supports date range filtering via start_date/end_date parameters or submittedDate in query.

    Args:
        params: Parameters including query string, pagination, sorting, and output format.

    Returns:
        String (markdown or JSON) with matched scholarly papers.

    Pagination:
        Use the 'start' parameter to page through large result sets.
        - First page: start=0, max_results=10
        - Second page: start=10, max_results=10
        - Third page: start=20, max_results=10
        Maximum: 2000 results per request, 100 recommended for agents.

    Query Examples:
        - Author + Title: 'au:del_maestro AND ti:checkerboard'
        - Author + Category: 'au:"Geoffrey Hinton" AND cat:stat.ML'
        - Category + Title: 'cat:math.HO AND ti:education'
        - Category only: 'cat:cs.AI'
        - Exclude terms: 'cat:cs.AI ANDNOT ti:neural'
        - Date range in query: 'au:"Geoffrey Hinton" AND cat:stat.ML AND submittedDate:[202001010000+TO+202212312359]'
    '''
    try:
        query_str = params.query
        if params.start_date and params.end_date:
            query_str += f" AND submittedDate:[{params.start_date}+TO+{params.end_date}]"
            
        api_params = {
            "search_query": query_str,
            "start": params.start,
            "max_results": params.max_results,
            "sortBy": params.sort_by.value,
            "sortOrder": params.sort_order.value
        }
        
        feed = await _make_api_request(api_params)
        
        papers = [_extract_paper_data(entry) for entry in feed.entries]
        total_results = int(feed.feed.get('opensearch_totalresults', 0))
        
        if params.response_format == ResponseFormat.MARKDOWN:
            return _format_papers_markdown(papers, f"Query '{params.query}'", total_results, params.start, params.max_results)
        else:
            return json.dumps({
                "query": params.query,
                "total_results": total_results,
                "start": params.start,
                "has_more": total_results > (params.start + len(papers)),
                "papers": papers
            }, indent=2)
            
    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="arxiv_search",
    annotations={
        "title": "Search arXiv",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def arxiv_search(params: ArxivSearchInput) -> str:
    '''Search across all fields in arXiv. Use for general keyword searches.

    This wraps 'all:[query]' for broad searches. For targeted searches combining
    multiple fields (e.g., author AND category), use arxiv_search_advanced instead.

    Args:
        params: Search keywords (e.g. 'electron thermal conductivity'), pagination, sort options.

    Returns:
        String (markdown or JSON) with matched papers.

    When to use which tool:
        - arxiv_search: General keyword search (e.g., "transformer attention")
        - arxiv_search_advanced: Combined filters (e.g., author + category + date)
        - arxiv_search_by_author: Papers by a specific author (with optional category)
        - arxiv_search_by_category: Papers in a category (with optional author)
    '''
    # Only prepend 'all:' if no field prefix is detected at the start
    if not any(params.query.startswith(f"{p}:") for p in ['ti', 'au', 'abs', 'co', 'jr', 'cat', 'rn', 'all']):
        params.query = f"all:{params.query}"
    return await arxiv_search_advanced(params)


@mcp.tool(
    name="arxiv_get_paper",
    annotations={
        "title": "Get Specific arXiv Papers",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def arxiv_get_paper(params: ArxivGetPaperInput) -> str:
    '''Fetch full metadata for one or more specific arXiv IDs. Use when you have the arXiv ID and need complete details.

    Returns all metadata including: title, authors (with affiliations), abstract, categories
    (primary and cross-list), publication dates, journal references, DOI, and links.

    Args:
        params: List of exact arXiv IDs (e.g., ["2401.12345", "1905.00001v1"])

    Returns:
        String (markdown or JSON) with complete paper metadata including abstracts.

    Version Support:
        Supports version-specific paper retrieval by appending version number:
        - '1706.03762' - retrieves latest version
        - '1706.03762v1' - retrieves version 1 specifically
        - '1706.03762v2' - retrieves version 2 specifically

    Example: Get details for a specific paper by ID:
        id_list=["2210.10318"]
    '''
    try:
        id_str = ",".join(params.id_list)
        api_params = {
            "id_list": id_str
        }
        
        feed = await _make_api_request(api_params)
        papers = [_extract_paper_data(entry) for entry in feed.entries]
        
        if params.response_format == ResponseFormat.MARKDOWN:
            if not papers:
                return f"No papers found for IDs: {id_str}"
            
            lines = [f"# arXiv Papers Retrieval", ""]
            for paper in papers:
                 lines.append(f"## {paper['title']}")
                 lines.append(f"**ID:** {paper['id']}")
                 lines.append(f"**Authors:** {', '.join(paper['authors'])}")
                 lines.append(f"**Published:** {paper['published']} | **Updated:** {paper['updated']}")
                 lines.append(f"**Categories:** {', '.join(paper['categories'])}")
                 if paper['journal_ref']:
                      lines.append(f"**Journal Ref:** {paper['journal_ref']}")
                 if paper['doi']:
                      lines.append(f"**DOI:** {paper['doi']}")
                 if paper['comment']:
                      lines.append(f"**Comment:** {paper['comment']}")
                 
                 lines.append(f"\n### Abstract\n{paper['summary']}\n")
                 lines.append(f"**Links:** [Abstract URL]({paper['abs_url']}) | [PDF URL]({paper['pdf_url']})\n---")
            return "\n".join(lines)
        else:
            return json.dumps({"papers": papers}, indent=2)

    except Exception as e:
        return _handle_api_error(e)


@mcp.tool(
    name="arxiv_search_by_author",
    annotations={
        "title": "Search Papers by Author",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def arxiv_search_by_author(params: ArxivAuthorSearchInput) -> str:
    '''Search for papers published by a specific author, optionally filtered by category.

    Use this when you need papers by a specific author. For combining with category,
    set the category parameter (e.g. category='stat.ML'). For date filtering, set
    start_date and end_date in YYYYMMDDHHMM format.

    Args:
        params: Author name, optional category filter, date range, pagination, and output format.

    Returns:
        String (markdown or JSON) with matched papers sorted by the specified criteria.

    Pagination:
        Use start parameter to page through results (e.g., start=0, then start=10, etc.)
        Maximum: 100 results per request for author searches.

    Example: Find Geoffrey Hinton papers in stat.ML between 2020-2022:
        author_name='Geoffrey Hinton', category='stat.ML',
        start_date='202001010000', end_date='202212312359'

    Note: Author name matching can vary. Try:
        - Full name: 'Geoffrey Hinton'
        - Last name only: 'Hinton'
        - With initials: 'G. Hinton'
    '''
    # Enclose author name in quotes for phrase matching
    query_str = f'au:"{params.author_name}"'

    # Add category filter if provided
    if params.category:
        query_str += f' AND cat:{params.category}'

    adv_params = ArxivSearchInput(
        query=query_str,
        start_date=params.start_date,
        end_date=params.end_date,
        start=params.start,
        max_results=params.max_results,
        sort_by=params.sort_by,
        sort_order=params.sort_order,
        response_format=params.response_format
    )
    return await arxiv_search_advanced(adv_params)


@mcp.tool(
    name="arxiv_search_by_category",
    annotations={
        "title": "Browse Category",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def arxiv_search_by_category(params: ArxivCategorySearchInput) -> str:
    '''Search or browse papers within a specific arXiv category, with optional author and date filtering.

    Use this to explore a category. For combining with author filtering, set the
    author_name parameter. For date filtering, set start_date and end_date.

    Args:
        params: Category ID, optional author filter, date range, pagination, and output format.

    Returns:
        String (markdown or JSON) with matched papers.

    Pagination:
        Use start parameter to page through results (e.g., page 1: start=0, page 2: start=10).
        Maximum: 100 results per request for category searches.

    Example: Find stat.ML papers by Geoffrey Hinton between 2020-2022:
        category='stat.ML', author_name='Geoffrey Hinton',
        start_date='202001010000', end_date='202212312359'
    '''
    query_str = f"cat:{params.category}"

    # Add author filter if provided
    if params.author_name:
        query_str += f' AND au:"{params.author_name}"'

    if params.start_date and params.end_date:
         query_str += f" AND submittedDate:[{params.start_date}+TO+{params.end_date}]"

    adv_params = ArxivSearchInput(
        query=query_str,
        start=params.start,
        max_results=params.max_results,
        sort_by=params.sort_by,
        sort_order=params.sort_order,
        response_format=params.response_format
    )
    return await arxiv_search_advanced(adv_params)


@mcp.tool(
    name="arxiv_get_pdf_url",
    annotations={
        "title": "Get PDF Download URL",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def arxiv_get_pdf_url(params: ArxivGetPdfUrlInput) -> str:
    '''Get the direct PDF download URL for a given arXiv ID.

    Does NOT download the PDF. Returns the URL you can use to download it.

    Args:
        params: arXiv ID (e.g., "2105.14321" or "2105.14321v1")
    '''
    # e.g. 2105.14321v1 -> https://arxiv.org/pdf/2105.14321v1.pdf
    pdf_url = f"https://arxiv.org/pdf/{params.paper_id}.pdf"
    
    return json.dumps({
        "id": params.paper_id,
        "pdf_url": pdf_url
    }, indent=2)


@mcp.tool(
    name="arxiv_list_categories",
    annotations={
        "title": "List arXiv Categories",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False
    }
)
async def arxiv_list_categories() -> str:
    '''Retrieve the complete arXiv subject taxonomy.

    Provides all categorized identifiers and human-readable names for field
    filtering (e.g., using 'cat:cs.AI' in arxiv_search_advanced).
    Note: A paper may appear in multiple categories. The primary_category field
    in search results indicates the main classification, but papers can also
    be tagged with secondary categories.
    '''
    categories = {
        "Computer Science": {
            "cs.AI": "Artificial Intelligence",
            "cs.AR": "Hardware Architecture",
            "cs.CC": "Computational Complexity",
            "cs.CE": "Computational Engineering, Finance, and Science",
            "cs.CG": "Computational Geometry",
            "cs.CL": "Computation and Language",
            "cs.CR": "Cryptography and Security",
            "cs.CV": "Computer Vision and Pattern Recognition",
            "cs.CY": "Computers and Society",
            "cs.DB": "Databases",
            "cs.DC": "Distributed, Parallel, and Cluster Computing",
            "cs.DL": "Digital Libraries",
            "cs.DM": "Discrete Mathematics",
            "cs.DS": "Data Structures and Algorithms",
            "cs.ET": "Emerging Technologies",
            "cs.FL": "Formal Languages and Automata Theory",
            "cs.GL": "General Literature",
            "cs.GR": "Graphics",
            "cs.GT": "Computer Science and Game Theory",
            "cs.HC": "Human-Computer Interaction",
            "cs.IR": "Information Retrieval",
            "cs.IT": "Information Theory",
            "cs.LG": "Machine Learning",
            "cs.LO": "Logic in Computer Science",
            "cs.MA": "Multiagent Systems",
            "cs.MM": "Multimedia",
            "cs.MS": "Mathematical Software",
            "cs.NA": "Numerical Analysis (alias for math.NA)",
            "cs.NE": "Neural and Evolutionary Computing",
            "cs.NI": "Networking and Internet Architecture",
            "cs.OH": "Other Computer Science",
            "cs.OS": "Operating Systems",
            "cs.PF": "Performance",
            "cs.PL": "Programming Languages",
            "cs.RO": "Robotics",
            "cs.SC": "Symbolic Computation",
            "cs.SD": "Sound",
            "cs.SE": "Software Engineering",
            "cs.SI": "Social and Information Networks",
            "cs.SY": "Systems and Control (alias for eess.SY)"
        },
        "Economics": {
            "econ.EM": "Econometrics",
            "econ.GN": "General Economics",
            "econ.TH": "Theoretical Economics"
        },
        "Electrical Engineering and Systems Science": {
            "eess.AS": "Audio and Speech Processing",
            "eess.IV": "Image and Video Processing",
            "eess.SP": "Signal Processing",
            "eess.SY": "Systems and Control"
        },
        "Mathematics": {
            "math.AC": "Commutative Algebra",
            "math.AG": "Algebraic Geometry",
            "math.AP": "Analysis of PDEs",
            "math.AT": "Algebraic Topology",
            "math.CA": "Classical Analysis and ODEs",
            "math.CO": "Combinatorics",
            "math.CT": "Category Theory",
            "math.CV": "Complex Variables",
            "math.DG": "Differential Geometry",
            "math.DS": "Dynamical Systems",
            "math.FA": "Functional Analysis",
            "math.GM": "General Mathematics",
            "math.GN": "General Topology",
            "math.GR": "Group Theory",
            "math.GT": "Geometric Topology",
            "math.HO": "History and Overview",
            "math.IT": "Information Theory (alias for cs.IT)",
            "math.KT": "K-Theory and Homology",
            "math.LO": "Logic",
            "math.MG": "Metric Geometry",
            "math.MP": "Mathematical Physics",
            "math.NA": "Numerical Analysis",
            "math.NT": "Number Theory",
            "math.OA": "Operator Algebras",
            "math.OC": "Optimization and Control",
            "math.PR": "Probability",
            "math.QA": "Quantum Algebra",
            "math.RA": "Rings and Algebras",
            "math.RT": "Representation Theory",
            "math.SG": "Symplectic Geometry",
            "math.SP": "Spectral Theory",
            "math.ST": "Statistics Theory"
        },
        "Astrophysics": {
            "astro-ph.CO": "Cosmology and Nongalactic Astrophysics",
            "astro-ph.EP": "Earth and Planetary Astrophysics",
            "astro-ph.GA": "Astrophysics of Galaxies",
            "astro-ph.HE": "High Energy Astrophysical Phenomena",
            "astro-ph.IM": "Instrumentation and Methods for Astrophysics",
            "astro-ph.SR": "Solar and Stellar Astrophysics"
        },
        "Condensed Matter": {
            "cond-mat.dis-nn": "Disordered Systems and Neural Networks",
            "cond-mat.mes-hall": "Mesoscale and Nanoscale Physics",
            "cond-mat.mtrl-sci": "Materials Science",
            "cond-mat.other": "Other Condensed Matter",
            "cond-mat.quant-gas": "Quantum Gases",
            "cond-mat.soft": "Soft Condensed Matter",
            "cond-mat.stat-mech": "Statistical Mechanics",
            "cond-mat.str-el": "Strongly Correlated Electrons",
            "cond-mat.supr-con": "Superconductivity"
        },
        "General Relativity and Quantum Cosmology": {
            "gr-qc": "General Relativity and Quantum Cosmology"
        },
        "High Energy Physics - Experiment": {
            "hep-ex": "High Energy Physics - Experiment"
        },
        "High Energy Physics - Lattice": {
            "hep-lat": "High Energy Physics - Lattice"
        },
        "High Energy Physics - Phenomenology": {
            "hep-ph": "High Energy Physics - Phenomenology"
        },
        "High Energy Physics - Theory": {
            "hep-th": "High Energy Physics - Theory"
        },
        "Mathematical Physics": {
            "math-ph": "Mathematical Physics"
        },
        "Nonlinear Sciences": {
            "nlin.AO": "Adaptation and Self-Organizing Systems",
            "nlin.CD": "Chaotic Dynamics",
            "nlin.CG": "Cellular Automata and Lattice Gases",
            "nlin.PS": "Pattern Formation and Solitons",
            "nlin.SI": "Exactly Solvable and Integrable Systems"
        },
        "Nuclear Experiment": {
            "nucl-ex": "Nuclear Experiment"
        },
        "Nuclear Theory": {
            "nucl-th": "Nuclear Theory"
        },
        "Physics": {
            "physics.acc-ph": "Accelerator Physics",
            "physics.ao-ph": "Atmospheric and Oceanic Physics",
            "physics.app-ph": "Applied Physics",
            "physics.atm-clus": "Atomic and Molecular Clusters",
            "physics.atom-ph": "Atomic Physics",
            "physics.bio-ph": "Biological Physics",
            "physics.chem-ph": "Chemical Physics",
            "physics.class-ph": "Classical Physics",
            "physics.comp-ph": "Computational Physics",
            "physics.data-an": "Data Analysis, Statistics and Probability",
            "physics.ed-ph": "Physics Education",
            "physics.flu-dyn": "Fluid Dynamics",
            "physics.gen-ph": "General Physics",
            "physics.geo-ph": "Geophysics",
            "physics.hist-ph": "History and Philosophy of Physics",
            "physics.ins-det": "Instrumentation and Detectors",
            "physics.med-ph": "Medical Physics",
            "physics.optics": "Optics",
            "physics.plasm-ph": "Plasma Physics",
            "physics.pop-ph": "Popular Physics",
            "physics.soc-ph": "Physics and Society",
            "physics.space-ph": "Space Physics"
        },
        "Quantum Physics": {
            "quant-ph": "Quantum Physics"
        },
        "Quantitative Biology": {
            "q-bio.BM": "Biomolecules",
            "q-bio.CB": "Cell Behavior",
            "q-bio.GN": "Genomics",
            "q-bio.MN": "Molecular Networks",
            "q-bio.NC": "Neurons and Cognition",
            "q-bio.OT": "Other Quantitative Biology",
            "q-bio.PE": "Populations and Evolution",
            "q-bio.QM": "Quantitative Methods",
            "q-bio.SC": "Subcellular Processes",
            "q-bio.TO": "Tissues and Organs"
        },
        "Quantitative Finance": {
            "q-fin.CP": "Computational Finance",
            "q-fin.EC": "Economics (alias for econ.GN)",
            "q-fin.GN": "General Finance",
            "q-fin.MF": "Mathematical Finance",
            "q-fin.PM": "Portfolio Management",
            "q-fin.PR": "Pricing of Securities",
            "q-fin.RM": "Risk Management",
            "q-fin.ST": "Statistical Finance",
            "q-fin.TR": "Trading and Market Microstructure"
        },
        "Statistics": {
            "stat.AP": "Applications",
            "stat.CO": "Computation",
            "stat.ME": "Methodology",
            "stat.ML": "Machine Learning",
            "stat.OT": "Other Statistics",
            "stat.TH": "Statistics Theory (alias for math.ST)"
        }
    }

    return json.dumps({"taxonomy": categories, "note": "Use category IDs (e.g., 'stat.ML', 'cs.AI', 'physics.bio-ph') as the cat: prefix in arxiv_search_advanced or the category parameter in arxiv_search_by_category."}, indent=2)


@mcp.tool(
    name="arxiv_get_latest",
    annotations={
        "title": "Get Latest Submissions",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True
    }
)
async def arxiv_get_latest(params: ArxivCategorySearchInput) -> str:
    '''Get the strictly latest submissions for a category, sorted by submission date (newest first).

    A convenience tool over category search that enforces sorting by
    submitted date descending.

    Args:
        params: Category ID (e.g. 'cs.AI', 'stat.ML'), limit.
    '''
    params.sort_by = SortBy.SUBMITTED_DATE
    params.sort_order = SortOrder.DESCENDING
    return await arxiv_search_by_category(params)


if __name__ == "__main__":
    mcp.run()
