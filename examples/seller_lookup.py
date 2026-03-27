"""
Example: Seller Storefront Lookup via Keepa.

Given one or more Amazon Seller IDs, extracts their full product catalog
from Keepa's Seller Storefront page. Returns list of dicts with 160+ columns
including Sales Rank, Buy Box price, category, subcategory ranks, etc.

Performance: ~9 seconds per seller (including CSV export).

Usage:
    # Single seller
    python examples/seller_lookup.py AVFHERP2L596L

    # Multiple sellers
    python examples/seller_lookup.py AVFHERP2L596L A3P5ROKL5A1OLE A1GD4LR25P9QFR

    # Save to CSV
    python examples/seller_lookup.py AVFHERP2L596L --output results.csv
"""

import asyncio
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from keepa_automation import KeepaAutomation

import pandas as pd


async def main():
    parser = argparse.ArgumentParser(description="Keepa Seller Storefront Lookup")
    parser.add_argument("seller_ids", nargs="+", help="Amazon Seller IDs (9-21 uppercase alphanumeric)")
    parser.add_argument("--output", "-o", help="Save results to CSV file")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")
    parser.add_argument("--delay", type=int, default=10, help="Delay between sellers in batch (seconds)")
    args = parser.parse_args()

    ka = KeepaAutomation(headless=args.headless)
    await ka.setup()
    await ka.login()

    try:
        if len(args.seller_ids) == 1:
            # Single seller — direct lookup
            products = await ka.seller_lookup(args.seller_ids[0])
            print(f"\nSeller {args.seller_ids[0]}: {len(products)} products")

            if products and args.output:
                df = pd.DataFrame(products)
                df.to_csv(args.output, index=False)
                print(f"Saved to {args.output}")
            elif products:
                print(f"Columns ({len(products[0])}): {list(products[0].keys())[:10]}...")
                for p in products[:3]:
                    title = p.get("Title", "N/A")
                    rank = p.get("Sales Rank: Current", "N/A")
                    print(f"  {title[:60]} | Rank: {rank}")
        else:
            # Batch — multiple sellers
            catalogs = await ka.seller_lookup_batch(
                args.seller_ids,
                delay_between=args.delay,
            )

            all_products = []
            for sid, products in catalogs.items():
                print(f"Seller {sid}: {len(products)} products")
                for p in products:
                    p["_seller_id"] = sid
                all_products.extend(products)

            print(f"\nTotal: {len(all_products)} products across {len(args.seller_ids)} sellers")

            if all_products and args.output:
                df = pd.DataFrame(all_products)
                df.to_csv(args.output, index=False)
                print(f"Saved to {args.output}")
    finally:
        await ka.close()


if __name__ == "__main__":
    asyncio.run(main())
