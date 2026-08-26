# Uber Eats Search MCP Server

An MCP (Model Context Protocol) server that lets AI agents search Uber Eats for food items, compare prices, and find the best deals — all through a headless anti-detection browser.

## What It Does

Two tools work together as a pipeline:

1. **`uber_eats_search`** — Searches Uber Eats for a food term near a delivery address. Launches a headless Camoufox browser, intercepts the `getSearchFeedV1` API to filter for restaurants only (no grocery stores), visits each restaurant's menu page, and extracts all menu items with prices from the embedded schema.org JSON-LD. Returns a combined JSON blob.

2. **`uber_eats_format`** — Filters and sorts the JSON into compact Markdown grouped by store, with delivery times, item names, prices, and BOGO status. Automatically removes pickup-only stores and stores with no delivery partners.

The agent translates item names from the local language when presenting results to the user.

## How It Works

```
User: "find me kung pao chicken"
  ↓
Agent calls uber_eats_search("宮保雞丁")     ← 5-10 min, visits ~30 restaurants
  ↓
Agent calls uber_eats_format(json, keywords=["宮保","雞"], sort="price")
  ↓
Agent translates and presents results:
  [岳泰式料理](url): 14 min
   - 宮保雞丁 (Kung Pao Chicken): NT$180, bogo?: false
```

## Key Design Decisions

- **Anti-detection browser**: Uses [Camoufox](https://github.com/nichochar/camoufox) (anti-detection Firefox) because Uber Eats blocks standard headless Chrome.
- **API interception**: Intercepts the `getSearchFeedV1` POST request and sets `vertical=RESTAURANTS` to exclude grocery stores, convenience stores, and markets — no hardcoded keyword filters.
- **Structured menu extraction**: Parses the schema.org Restaurant JSON-LD embedded in each store page's HTML for clean, structured menu data (name, price, description, section).
- **Two-script pattern**: Data collection (slow, browser-based) is separated from formatting (instant, pure data processing). The agent stays out of the data path.
- **Trailing spaces for Markdown**: Each output line ends with two trailing spaces so Discord and similar platforms render tight line breaks instead of paragraph breaks.

## Installation

### Prerequisites

- Python 3.11+
- A Hermes Agent instance (or any MCP-compatible client)

### Install dependencies

```bash
# Use the Hermes venv or create a new one
pip install mcp camoufox[geoip] playwright

# Download the Camoufox browser binary
camoufox fetch
```

### Configuration

Create `uber_eats_config.json` in the same directory as the server script:

```json
{
  "default_address": "Mandarin Oriental Taipei",
  "max_stores": 30,
  "output_dir": "/tmp",
  "locale": "tw-en",
  "city_url": "https://www.ubereats.com/tw-en/city/taipei-tpe"
}
```

| Key | Description | Default |
|-----|-------------|---------|
| `default_address` | Default delivery address (Google Places query) | `Mandarin Oriental Taipei` |
| `max_stores` | Max restaurant pages to visit per search | `30` |
| `output_dir` | Directory for output files | `/tmp` |
| `locale` | Uber Eats locale | `tw-en` |
| `city_url` | Uber Eats city page URL | Taipei city page |

### Add to Hermes Agent

Add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  uber-eats-search:
    command: /path/to/python
    args:
      - /path/to/uber_eats_mcp_server.py
    enabled: true
```

Or use the CLI:

```bash
hermes config set mcp_servers.uber-eats-search.command /path/to/python
hermes config set mcp_servers.uber-eats-search.args '["/path/to/uber_eats_mcp_server.py"]'
hermes config set mcp_servers.uber-eats-search.enabled true
```

## Files

| File | Purpose |
|------|---------|
| `uber_eats_mcp_server.py` | MCP server entry point — registers tools and handles stdio transport |
| `uber_eats_search.py` | Data collection script — browser automation, API interception, menu extraction |
| `uber_eats_format.py` | Filter and format script — pure data processing, no browser |
| `uber_eats_config.json` | Environment config — address, max stores, locale |

## Tool Reference

### uber_eats_search

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `search_term` | string | Yes | Food item to search for (English or local language) |
| `address` | string | No | Delivery address (defaults to config) |
| `max_stores` | int | No | Max restaurant pages to visit (defaults to config) |

Returns JSON with `stores` (metadata) and `items` (all menu items across all stores).

If 0 stores are found, the search term likely didn't match. The response includes a `hint` field suggesting: try the local language name, try a broader category, or try a different transliteration.

### uber_eats_format

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `data_json` | string | Yes | JSON from uber_eats_search |
| `keywords` | string[] | No | Filter items by keywords (AND logic) |
| `match_mode` | string | No | `name` (default), `description`, or `any` |
| `bogo_only` | bool | No | Only show BOGO stores |
| `max_price` | number | No | Max price in TWD |
| `min_price` | number | No | Min price in TWD |
| `sort` | string | No | `price` (default), `delivery`, `store`, or `name` |
| `limit` | int | No | Max items to display (default 50) |
| `exclude_free` | bool | No | Exclude NT$0 items (default true) |

Returns Markdown string.

## Limitations

- Each restaurant page takes 10-15 seconds to load. Searching 30 stores takes ~5-10 minutes.
- Uber Eats sometimes serves bot challenge pages. The search script skips stores it can't load.
- Search results vary between page loads — Uber Eats rotates the store list.
- BOGO (Buy 1 Get 1) is a store-level badge — it means the store has *some* BOGO items, not that all items are BOGO.
- Must use a venv with both `camoufox` and `mcp` installed.

## License

MIT