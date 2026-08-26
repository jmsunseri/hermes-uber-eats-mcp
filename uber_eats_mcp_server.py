#!/usr/bin/env python3
"""
Uber Eats MCP Server

An MCP (Model Context Protocol) server exposing two tools for searching
Uber Eats and formatting results. Designed for use with Hermes Agent or
any MCP-compatible client.

## Tools

### uber_eats_search
Searches Uber Eats for a food item near a delivery address. Launches a
headless anti-detection browser (Camoufox), intercepts the getSearchFeedV1
API call to filter for restaurants only (excluding grocery stores), visits
each restaurant's menu page, and extracts all menu items with prices from
the embedded schema.org Restaurant JSON-LD.

Returns a JSON string with store metadata and all menu items combined.

### uber_eats_format
Reads the JSON output from uber_eats_search and filters/sorts/formats it
into Markdown suitable for display in chat platforms. Automatically filters
out pickup-only stores and stores with no delivery partners.

## Information Flow

The intended workflow is a two-step pipeline:

1. Agent calls uber_eats_search with a food search term and delivery address.
   This takes ~5-10 minutes as it visits each restaurant page.
   Returns raw JSON with all stores and all menu items.

2. Agent calls uber_eats_format with the JSON, keywords to filter by,
   and sorting preferences.
   Returns compact Markdown with store links, delivery times, item names,
   prices, and BOGO status.

The agent should translate Chinese item names to English when presenting
results to the user. The format tool preserves original names; translation
is the agent's responsibility.

## Important Notes for Agents

- If uber_eats_search returns 0 stores, the search term may not match any
  restaurants. Try alternative terms — the search matches against the local
  language (e.g., use Chinese names for dishes in Taipei). If the user gives
  an English dish name and you get 0 results, try the local language name,
  or try a broader category like "Chinese" and then filter menu items by
  the specific dish name using uber_eats_format keywords.

- The search intercepts Uber Eats' API and sets vertical=RESTAURANTS to
  exclude grocery stores, convenience stores, and markets automatically.

- The format tool automatically filters out pickup-only stores and stores
  with no delivery partners nearby.

- BOGO (Buy 1 Get 1) is a store-level badge from the search API. It means
  the store has SOME BOGO items, not that all items are BOGO.

## Configuration

The server reads a config file at the same directory as this script:
uber_eats_config.json with these keys:
- default_address: Default delivery address (Google Places query)
- max_stores: Maximum stores to visit per search (default 30)
- output_dir: Directory for output JSON files
- locale: Uber Eats locale (default tw-en)
- city_url: Uber Eats city page URL

## Requirements

- Python 3.11+
- mcp >= 2.0.0 (Model Context Protocol Python SDK)
- camoufox[geoip] (anti-detection Firefox browser)
- playwright (browser automation, installed with camoufox)

Install:
    pip install mcp camoufox[geoip]
    camoufox fetch  # download the browser binary

## Hermes Agent Configuration

Add to ~/.hermes/config.yaml under mcp_servers:

  mcp_servers:
    uber-eats-search:
      command: /home/user/.hermes/hermes-agent/venv/bin/python
      args:
        - /home/user/.hermes/scripts/uber_eats_mcp_server.py
      enabled: true

The venv must have camoufox and mcp installed.
"""

import json
import os
import sys

# Add the scripts directory to path so we can import the search/format modules
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

# Load config
CONFIG_PATH = os.path.join(SCRIPT_DIR, "uber_eats_config.json")
DEFAULT_CONFIG = {
    "default_address": "Mandarin Oriental Taipei",
    "max_stores": 30,
    "output_dir": "/tmp",
    "locale": "tw-en",
    "city_url": "https://www.ubereats.com/tw-en/city/taipei-tpe",
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG


# Tool descriptions — long and detailed for the agent
SEARCH_TOOL_DESC = """Search Uber Eats for a food item and collect all menu items from restaurants near a delivery address.

This launches a headless anti-detection browser, searches Uber Eats, visits each restaurant's menu page, and extracts all items with prices. Grocery stores, convenience stores, and markets are automatically excluded.

Store selection by priority reduces search time from 5-10 minutes to 1-2 minutes:
- 'fast': Top 5 fastest delivery times (~1 min)
- 'quality': Top 5 highest rated restaurants (~1 min)
- 'balanced': Top 3 by delivery time + top 3 by rating, deduped (~1-2 min) — DEFAULT
- 'none': Visit all matching stores (~5-10 min)

BEFORE calling this tool, determine the user's urgency:
- If the user says they're in a hurry, rushing, or want food fast → priority='fast'
- If the user says they want good food, best quality, or don't care about time → priority='quality'
- If the user doesn't specify, or says they don't care → priority='balanced' (default)
- Only use priority='none' if the user explicitly wants comprehensive results

If 0 stores are found, the search term likely didn't match. Try:
- The local language name (e.g., Chinese for Taipei: use '宮保雞丁' not 'kung pao chicken')
- A broader category (e.g., 'Chinese' instead of a specific dish)
- A different spelling or transliteration

Returns JSON: {search_term, address, timestamp, stores: [...], items: [...]}"""

FORMAT_TOOL_DESC = """Filter and format Uber Eats search results into compact Markdown for display.

Reads JSON from uber_eats_search and outputs Markdown grouped by store with delivery times, item names, prices, and BOGO status. Automatically filters out pickup-only stores and stores with no delivery partners.

Each line ends with two trailing spaces for proper Markdown rendering on Discord and similar platforms.

The agent SHOULD translate Chinese item names to English when presenting results to the user. This tool preserves original names; translation is the agent's job.

If no items match the filters, returns 'No items found matching your criteria.' — try broader keywords or fewer filters."""

# Initialize MCP server
app = Server("uber-eats-search")


# Register tool list handler
async def list_tools(ctx, request: types.ListToolsRequest) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="uber_eats_search",
                description=SEARCH_TOOL_DESC,
                input_schema={
                    "type": "object",
                    "properties": {
                        "search_term": {
                            "type": "string",
                            "description": "Food item to search for. Can be English or local language. If 0 results with English, try the local language name (e.g. Chinese for Taipei).",
                        },
                        "address": {
                            "type": "string",
                            "description": "Delivery address as a Google Places query (e.g. 'Mandarin Oriental Taipei', 'Taipei 101'). Defaults to config file address.",
                        },
                        "max_stores": {
                            "type": "integer",
                            "description": "Max restaurant pages to visit. Each takes 10-15 seconds. Default from config (30).",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["fast", "quality", "balanced", "none"],
                            "description": "Store selection priority. 'fast' = top 5 by delivery time. 'quality' = top 5 by rating. 'balanced' = top 3 by delivery + top 3 by rating (default). 'none' = all stores. Determine from user urgency before calling.",
                        },
                    },
                    "required": ["search_term"],
                },
            ),
            types.Tool(
                name="uber_eats_format",
                description=FORMAT_TOOL_DESC,
                input_schema={
                    "type": "object",
                    "properties": {
                        "data_json": {
                            "type": "string",
                            "description": "JSON string from uber_eats_search output.",
                        },
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Keywords to filter items by. ALL must be present (AND logic). English or local language. e.g. ['宮保', '雞'] or ['sparkling', 'americano'].",
                        },
                        "match_mode": {
                            "type": "string",
                            "enum": ["name", "description", "any"],
                            "description": "Where to match keywords: 'name' (item name only, default), 'description' (name+description), 'any' (name+description+section).",
                        },
                        "bogo_only": {
                            "type": "boolean",
                            "description": "Only show items from stores with a BOGO badge.",
                        },
                        "max_price": {
                            "type": "number",
                            "description": "Maximum price in TWD. Items above this are excluded.",
                        },
                        "min_price": {
                            "type": "number",
                            "description": "Minimum price in TWD. Items below this are excluded.",
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["price", "delivery", "store", "name"],
                            "description": "Sort order: 'price' (default), 'delivery', 'store', or 'name'.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of items to display.",
                        },
                        "exclude_free": {
                            "type": "boolean",
                            "description": "Exclude NT$0 items (notices, instructions).",
                        },
                    },
                    "required": ["data_json"],
                },
            ),
        ]
    )


app.add_request_handler("tools/list", types.ListToolsRequest, list_tools)


# Register tool call handler
async def call_tool_handler(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
    name = params.name
    arguments = params.arguments or {}

    if name == "uber_eats_search":
        content = _do_search(arguments)
        return types.CallToolResult(content=content)
    elif name == "uber_eats_format":
        content = _do_format(arguments)
        return types.CallToolResult(content=content)
    else:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {name}")],
            is_error=True,
        )


app.add_request_handler("tools/call", types.CallToolRequestParams, call_tool_handler)


def _do_search(arguments: dict) -> list[types.TextContent]:
    from uber_eats_search import search_uber_eats

    config = load_config()
    search_term = arguments.get("search_term", "")
    address = arguments.get("address") or config.get("default_address", "Taipei 101")
    max_stores = arguments.get("max_stores") or config.get("max_stores", 30)
    priority = arguments.get("priority", "balanced")

    result = search_uber_eats(search_term, address, max_stores, priority=priority)

    store_count = len(result.get("stores", []))
    item_count = len(result.get("items", []))

    # If no stores found, add guidance for the agent
    if store_count == 0:
        result["hint"] = (
            "No restaurants found. The search term likely didn't match any stores. "
            "Try: (1) the local language name for the dish, (2) a broader category "
            "like 'Chinese' or 'Thai', (3) a different transliteration. "
            "Then use uber_eats_format with keywords to filter for the specific dish."
        )

    return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


def _do_format(arguments: dict) -> list[types.TextContent]:
    from uber_eats_format import (
        parse_price,
        parse_delivery_minutes,
        item_matches,
        format_markdown,
    )

    data = json.loads(arguments["data_json"])
    items = data.get("items", [])

    # Filter: exclude pickup-only and unavailable stores
    items = [
        i for i in items
        if i.get("store_url", "") and "diningMode=PICKUP" not in i.get("store_url", "")
    ]
    items = [
        i for i in items
        if i.get("delivery_time", "") and "No delivery" not in i.get("delivery_time", "")
    ]

    # Filter: exclude free items
    if arguments.get("exclude_free", True):
        items = [i for i in items if parse_price(i.get("price", 0)) > 0]

    # Filter: keywords
    keywords = arguments.get("keywords")
    if keywords:
        match_mode = arguments.get("match_mode", "name")
        items = [i for i in items if item_matches(i, keywords, match_mode)]

    # Filter: BOGO only
    if arguments.get("bogo_only", False):
        items = [i for i in items if i.get("has_bogo")]

    # Filter: price range
    max_price = arguments.get("max_price")
    if max_price is not None:
        items = [i for i in items if parse_price(i.get("price", 0)) <= max_price]
    min_price = arguments.get("min_price")
    if min_price is not None:
        items = [i for i in items if parse_price(i.get("price", 0)) >= min_price]

    # Sort
    sort = arguments.get("sort", "price")
    if sort == "price":
        items.sort(key=lambda i: (i.get("store_name", ""), parse_price(i.get("price", 0))))
    elif sort == "delivery":
        items.sort(key=lambda i: (parse_delivery_minutes(i.get("delivery_time", "")), i.get("store_name", "")))
    elif sort == "store":
        items.sort(key=lambda i: i.get("store_name", ""))
    elif sort == "name":
        items.sort(key=lambda i: i.get("name", ""))

    # Limit
    limit = arguments.get("limit", 50)
    items = items[:limit]

    return [types.TextContent(type="text", text=format_markdown(items))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())