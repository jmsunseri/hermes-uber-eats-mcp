#!/usr/bin/env python3
"""
Uber Eats Item Formatter — Filter and format results for display

Reads a JSON file produced by uber_eats_search.py and outputs a
filtered, sorted, Markdown-formatted list of items grouped by store.

Usage:
    python uber_eats_format.py results.json --keyword "sparkling" --keyword "americano" --sort price
    python uber_eats_format.py results.json --bogo-only --sort price
    python uber_eats_format.py results.json --max-price 200 --sort delivery

The formatter is a pure data processor — no browser, no network calls.
It reads the JSON, filters items, and outputs Markdown grouped by store.
Translation is handled by the caller (the LLM), not by this script.
"""

import argparse
import json
import re
import sys


def parse_price(price_str: str) -> float:
    """Parse a price string like '160.00' or 'NT$160' into a float."""
    if not price_str:
        return 0.0
    match = re.search(r'[\d.]+', str(price_str))
    return float(match.group()) if match else 0.0


def parse_delivery_minutes(delivery_str: str) -> int:
    """Parse delivery time like '19 min' or '11:52AM' into approximate minutes."""
    if not delivery_str:
        return 9999
    if re.match(r'\d{1,2}:\d{2}', delivery_str):
        return 0
    match = re.search(r'(\d+)\s*min', delivery_str)
    return int(match.group(1)) if match else 9999


def item_matches(item: dict, keywords: list[str], match_mode: str) -> bool:
    """
    Check if an item matches the given keywords.
    Searches the item name (and optionally description) for ALL keywords.
    Keywords can be English or Chinese.
    """
    name = item.get("name", "").lower()
    desc = item.get("description", "").lower()
    section = item.get("section", "").lower()

    if match_mode == "name":
        text = name
    elif match_mode == "description":
        text = f"{name} {desc}"
    else:
        text = f"{name} {desc} {section}"

    return all(kw.lower() in text for kw in keywords) if keywords else True


def format_markdown(items: list[dict]) -> str:
    """Format filtered items into Markdown grouped by store."""
    if not items:
        return "No items found matching your criteria."

    # Group items by store
    stores = {}  # store_name -> {url, delivery, items}
    for item in items:
        store_name = item.get("store_name", "Unknown")
        if store_name not in stores:
            stores[store_name] = {
                "url": item.get("store_url", ""),
                "delivery": item.get("delivery_time", ""),
                "has_bogo": item.get("has_bogo", False),
                "items": [],
            }
        stores[store_name]["items"].append(item)

    # Sort stores by delivery time (fastest first), then by name
    sorted_stores = sorted(
        stores.items(),
        key=lambda s: (parse_delivery_minutes(s[1]["delivery"]), s[0])
    )

    lines = []
    for store_name, store_data in sorted_stores:
        url = store_data["url"]
        delivery = store_data["delivery"]
        if delivery:
            lines.append(f"[{store_name}]({url}): {delivery}  ")
        else:
            lines.append(f"[{store_name}]({url})  ")
        for item in store_data["items"]:
            name = item.get("name", "")
            price = f"NT${parse_price(item.get('price', 0)):.0f}"
            bogo = "true" if item.get("has_bogo") else "false"
            lines.append(f" - {name}: {price}, bogo?: {bogo}  ")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Format Uber Eats search results")
    parser.add_argument("input", help="Input JSON file from uber_eats_search.py")
    parser.add_argument("--keyword", "-k", action="append", default=[],
                        help="Filter by keyword (can repeat). Item must contain ALL keywords. Works with English or Chinese terms.")
    parser.add_argument("--match-mode", "-m", choices=["name", "description", "any"],
                        default="name", help="Where to match keywords (default: name only)")
    parser.add_argument("--bogo-only", action="store_true", help="Only show items from stores with BOGO")
    parser.add_argument("--max-price", type=float, help="Maximum price in TWD")
    parser.add_argument("--min-price", type=float, help="Minimum price in TWD")
    parser.add_argument("--sort", choices=["price", "delivery", "store", "name"],
                        default="price", help="Sort order (default: price)")
    parser.add_argument("--limit", "-n", type=int, default=50, help="Max items to display")
    parser.add_argument("--json", action="store_true", help="Output as JSON instead of Markdown")
    parser.add_argument("--exclude-free", action="store_true",
                        help="Exclude items with price 0 (notices, instructions, etc.)")
    args = parser.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data.get("items", [])

    # Filter: exclude pickup-only and unavailable stores
    # Pickup-only stores have diningMode=PICKUP in the URL
    # Unavailable stores have "No delivery" or empty delivery_time
    items = [i for i in items if i.get("store_url", "") and "diningMode=PICKUP" not in i.get("store_url", "")]
    items = [i for i in items if i.get("delivery_time", "") and "No delivery" not in i.get("delivery_time", "")]

    # Filter: exclude free items
    if args.exclude_free:
        items = [i for i in items if parse_price(i.get("price", 0)) > 0]

    # Filter: keywords
    if args.keyword:
        items = [i for i in items if item_matches(i, args.keyword, args.match_mode)]

    # Filter: BOGO only
    if args.bogo_only:
        items = [i for i in items if i.get("has_bogo")]

    # Filter: price range
    if args.max_price is not None:
        items = [i for i in items if parse_price(i.get("price", 0)) <= args.max_price]
    if args.min_price is not None:
        items = [i for i in items if parse_price(i.get("price", 0)) >= args.min_price]

    # Sort (within store groups for Markdown output)
    if args.sort == "price":
        items.sort(key=lambda i: (i.get("store_name", ""), parse_price(i.get("price", 0))))
    elif args.sort == "delivery":
        items.sort(key=lambda i: (parse_delivery_minutes(i.get("delivery_time", "")), i.get("store_name", "")))
    elif args.sort == "store":
        items.sort(key=lambda i: i.get("store_name", ""))
    elif args.sort == "name":
        items.sort(key=lambda i: i.get("name", ""))

    # Limit
    items = items[:args.limit]

    # Output
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:
        print(format_markdown(items))


if __name__ == "__main__":
    main()