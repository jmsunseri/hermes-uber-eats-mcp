#!/usr/bin/env python3
"""
Uber Eats Item Finder — Data Collection Script

Searches Uber Eats for a given food term near a given address,
visits each store's menu page, extracts all menu items with prices,
and saves the combined results as a JSON file.

Usage:
    python uber_eats_search.py "citrus americano" "Mandarin Oriental Taipei" --output results.json

Requirements:
    pip install camoufox[geoip] playwright

The script uses Camoufox (anti-detection Firefox) to bypass Uber Eats'
bot detection. It captures two data sources:
1. getSearchFeedV1 API — store list with delivery times, BOGO badges, URLs
2. Restaurant JSON-LD (schema.org) embedded in each store page — full menu items with prices
"""

import argparse
import json
import os
import re
import sys
import time
from urllib.parse import unquote

from camoufox.sync_api import Camoufox


def decode_unicode_escapes(text: str) -> str:
    """Decode \\uXXXX sequences in a string."""
    return re.sub(r'\\u([0-9a-fA-F]{4})', lambda m: chr(int(m.group(1), 16)), text)


def extract_store_list(page, address: str, search_term: str) -> list[dict]:
    """
    Extract the list of stores from the search results by intercepting
    the getSearchFeedV1 API response.
    """
    captured = []

    def on_response(response):
        if "getSearchFeedV1" in response.url and response.status == 200:
            try:
                captured.append(response.text())
            except Exception:
                pass

    page.on("response", on_response)

    # Set delivery address
    addr_input = page.locator("input").first
    addr_input.fill(address)
    time.sleep(4)
    suggestions = page.locator('[role="option"]')
    if suggestions.count() > 0:
        suggestions.first.click()
        time.sleep(3)

    # Enter search term
    search_input = page.locator("#search-suggestions-typeahead-input")
    page.evaluate('document.getElementById("search-suggestions-typeahead-input").focus()')
    search_input.fill(search_term)
    time.sleep(3)
    search_input.press("Enter")
    time.sleep(8)

    # Scroll to load more results
    for _ in range(3):
        page.evaluate("window.scrollBy(0, 2500)")
        time.sleep(2)

    if not captured:
        return []

    data = json.loads(captured[0])
    stores = []

    for item in data.get("data", {}).get("feedItems", []):
        if item.get("type") != "MINI_STORE_WITH_ITEMS":
            continue

        store_obj = item.get("miniStoreWithItems", {}).get("store", {})

        # Store name
        name = store_obj.get("title", {}).get("text", "")

        # Delivery time
        delivery_time = ""
        for meta in store_obj.get("meta", []):
            if meta.get("badgeType") == "ETD":
                delivery_time = meta.get("text", "")

        # BOGO badge
        has_bogo = any(
            "Buy 1, get 1" in m.get("text", "")
            for m in store_obj.get("meta2", [])
        )

        # Store URL
        action_url = store_obj.get("actionUrl", "")
        full_url = f"https://www.ubereats.com{action_url}" if action_url else ""

        # Rating
        rating = ""
        for meta in store_obj.get("meta", []):
            if meta.get("badgeType") == "RATING":
                rating = meta.get("text", "")

        stores.append({
            "name": name,
            "delivery_time": delivery_time,
            "has_bogo": has_bogo,
            "rating": rating,
            "url": full_url,
            "store_id": store_obj.get("storeUuid", ""),
        })

    return stores


def extract_menu_items(page) -> list[dict]:
    """
    Extract all menu items from a store page by parsing the
    Restaurant JSON-LD (schema.org) embedded in a script tag.

    The JSON-LD contains hasMenu → hasMenuSection → hasMenuItem
    with name, description, and offers.price for each item.
    """
    html = page.content()

    # Find all script tags
    script_matches = re.findall(r'<script[^>]*>(.*?)</script>', html, re.DOTALL)

    for script in script_matches:
        if '"Restaurant"' not in script or "hasMenuSection" not in script:
            continue
        if len(script) > 50000:
            continue  # Skip the big relay state blob

        # Decode \uXXXX escapes
        decoded = decode_unicode_escapes(script.strip())

        try:
            data = json.loads(decoded)
        except json.JSONDecodeError:
            continue

        menu = data.get("hasMenu", {})
        if not menu:
            continue

        sections = menu.get("hasMenuSection", [])
        items = []

        for sec in sections:
            sec_name = sec.get("name", "")
            for item in sec.get("hasMenuItem", []):
                offers = item.get("offers", {})
                items.append({
                    "section": sec_name,
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "price": offers.get("price", ""),
                    "currency": offers.get("priceCurrency", ""),
                })

        return items

    return []


def search_uber_eats(search_term: str, address: str, max_stores: int = 50) -> dict:
    """
    Main function: search Uber Eats for a food term, visit each store,
    and return a combined JSON with all menu items.

    Returns a dict with:
    - "search_term": the search query
    - "address": the delivery address
    - "timestamp": when the search was performed
    - "stores": list of store objects with name, delivery_time, has_bogo, url
    - "items": combined list of all menu items across all stores, each with
      store_name, store_url, delivery_time, has_bogo, section, name, price, currency
    """
    result = {
        "search_term": search_term,
        "address": address,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "stores": [],
        "items": [],
    }

    with Camoufox(headless=True, geoip=True) as browser:
        page = browser.new_page()

        # Capture the search feed API response — set up before navigation
        captured_api = []

        def on_response(response):
            if "getSearchFeedV1" in response.url and response.status == 200:
                try:
                    captured_api.append(response.text())
                except Exception:
                    pass

        page.on("response", on_response)

        # Step 1: Load the city page and get the store list
        print(f"Searching Uber Eats for '{search_term}' near '{address}'...", file=sys.stderr)
        page.goto("https://www.ubereats.com/tw-en/city/taipei-tpe", timeout=45000)
        time.sleep(8)

        # Intercept the getSearchFeedV1 API call: set vertical=RESTAURANTS
        # (excludes grocery stores) AND capture the response in one place.
        # Using route interception for both modification and capture, since
        # page.on("response") may not fire for intercepted requests.
        # Set up AFTER initial page load so it only affects the search request.
        def handle_route(route):
            request = route.request
            if "getSearchFeedV1" in request.url and request.method == "POST":
                body = request.post_data
                if body and '"vertical":"ALL"' in body:
                    body = body.replace('"vertical":"ALL"', '"vertical":"RESTAURANTS"')
                # Fulfill with the modified request and capture the response
                response = route.fetch(post_data=body)
                try:
                    captured_api.append(response.text())
                except Exception:
                    pass
                route.fulfill(response=response)
                return
            route.continue_()

        page.route("**/getSearchFeedV1*", handle_route)

        # Set address
        addr_input = page.locator("input").first
        addr_input.fill(address)
        time.sleep(4)
        suggestions = page.locator('[role="option"]')
        if suggestions.count() > 0:
            suggestions.first.click()
            time.sleep(3)

        # Search
        search_input = page.locator("#search-suggestions-typeahead-input")
        page.evaluate('document.getElementById("search-suggestions-typeahead-input").focus()')
        search_input.fill(search_term)
        time.sleep(3)
        search_input.press("Enter")
        time.sleep(8)

        # Scroll to load more
        for _ in range(3):
            page.evaluate("window.scrollBy(0, 2500)")
            time.sleep(2)

        # Parse the search feed API response
        if not captured_api:
            print("Warning: No search feed API response captured", file=sys.stderr)
            return result

        feed_data = json.loads(captured_api[0])
        stores = []

        for item in feed_data.get("data", {}).get("feedItems", []):
            # Handle all store types from the search feed API
            if item.get("type") == "MINI_STORE_WITH_ITEMS":
                store_obj = item.get("miniStoreWithItems", {}).get("store", {})
            elif item.get("type") in ("REGULAR_STORE", "REGULAR_STORE_WITH_ITEMS"):
                store_obj = item.get("store", {})
            else:
                continue

            name = store_obj.get("title", {}).get("text", "")
            delivery_time = ""
            for meta in store_obj.get("meta", []):
                if meta.get("badgeType") == "ETD":
                    delivery_time = meta.get("text", "")

            # BOGO can appear in meta2 (MINI_STORE_WITH_ITEMS) or signposts (REGULAR_STORE)
            has_bogo = any(
                "Buy 1, get 1" in m.get("text", "")
                for m in store_obj.get("meta2", [])
            )
            # Also check signposts field for REGULAR_STORE type
            if not has_bogo:
                signposts = store_obj.get("signposts", [])
                if signposts:
                    has_bogo = any("Buy 1, get 1" in s.get("text", "") for s in signposts)

            action_url = store_obj.get("actionUrl", "")
            full_url = f"https://www.ubereats.com{action_url}" if action_url else ""

            rating = ""
            for meta in store_obj.get("meta", []):
                if meta.get("badgeType") == "RATING":
                    rating = meta.get("text", "")

            stores.append({
                "name": name,
                "delivery_time": delivery_time,
                "has_bogo": has_bogo,
                "rating": rating,
                "url": full_url,
            })

        result["stores"] = stores
        print(f"Found {len(stores)} stores", file=sys.stderr)

        # Step 2: Visit each store page and extract menu items
        all_items = []
        for i, store in enumerate(stores[:max_stores]):
            if not store["url"]:
                continue

            print(f"  [{i+1}/{min(len(stores), max_stores)}] {store['name']}", file=sys.stderr)

            try:
                page.goto(store["url"], timeout=45000)
                time.sleep(10)  # Wait for page to load

                # Check if we hit a bot challenge
                text = page.evaluate("document.body.innerText")
                if len(text) < 500 or "還剩" in text:
                    print(f"    Blocked by challenge, skipping", file=sys.stderr)
                    continue

                items = extract_menu_items(page)

                for item in items:
                    item["store_name"] = store["name"]
                    item["store_url"] = store["url"]
                    item["delivery_time"] = store["delivery_time"]
                    item["has_bogo"] = store["has_bogo"]
                    all_items.append(item)

                print(f"    → {len(items)} items", file=sys.stderr)

            except Exception as e:
                print(f"    Error: {e}", file=sys.stderr)
                continue

        result["items"] = all_items
        print(f"\nTotal items collected: {len(all_items)}", file=sys.stderr)

    return result


def load_config() -> dict:
    """Load config from ~/.hermes/scripts/uber_eats_config.json."""
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uber_eats_config.json")
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


if __name__ == "__main__":
    config = load_config()

    parser = argparse.ArgumentParser(description="Search Uber Eats for food items")
    parser.add_argument("search_term", help="Food item to search for")
    parser.add_argument("--address", "-a", default=None,
                        help="Delivery address (default: from config file)")
    parser.add_argument("--output", "-o", default=None,
                        help="Output JSON file (default: /tmp/uber_eats_<search>.json)")
    parser.add_argument("--max-stores", "-m", type=int, default=None,
                        help="Max stores to visit (default: from config)")
    args = parser.parse_args()

    # Fall back to config for address, output, and max_stores
    address = args.address or config.get("default_address", "Taipei 101")
    max_stores = args.max_stores or config.get("max_stores", 50)
    output = args.output or os.path.join(
        config.get("output_dir", "/tmp"),
        f"uber_eats_{args.search_term.replace(' ', '_')}.json"
    )

    result = search_uber_eats(args.search_term, address, max_stores)

    with open(output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"\nResults saved to {output}")
    print(f"  Stores: {len(result['stores'])}")
    print(f"  Items: {len(result['items'])}")