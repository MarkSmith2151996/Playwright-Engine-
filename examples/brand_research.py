"""
Example: Research supplier contact info for brands.

Shows how to use SupplierResearcher with an existing Playwright page
(same browser session as Keepa automation).

Usage:
    python examples/brand_research.py
    python examples/brand_research.py --brands "Kinco,Stanley,DeWalt"
    python examples/brand_research.py --csv output.csv
"""

import asyncio
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from supplier_research import SupplierResearcher


async def main():
    parser = argparse.ArgumentParser(description="Research supplier contacts for brands")
    parser.add_argument(
        "--brands",
        default="Kinco",
        help="Comma-separated brand names (default: Kinco)",
    )
    parser.add_argument(
        "--category",
        default="",
        help="Product category for all brands (optional)",
    )
    parser.add_argument(
        "--csv",
        default="",
        help="Output CSV path (optional)",
    )
    parser.add_argument(
        "--claude",
        action="store_true",
        help="Enable Claude CLI fallback for ambiguous results",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless",
    )
    args = parser.parse_args()

    brands = [b.strip() for b in args.brands.split(",") if b.strip()]

    # Launch browser
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=args.headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context()
    page = await context.new_page()

    stealth = Stealth()
    await stealth.apply_stealth_async(page)

    # Create researcher
    researcher = SupplierResearcher(
        page,
        use_claude=args.claude,
        screenshot_dir="supplier_screenshots",
    )

    # Research brands
    if len(brands) == 1:
        # Single brand — use research() directly
        result = await researcher.research(brands[0], args.category)
        _print_result(result)
        results = [result]
    else:
        # Multiple brands — use batch API
        brand_list = [{"brand": b, "category": args.category} for b in brands]
        results = await researcher.research_batch(
            brand_list,
            progress_callback=lambda i, total, r: print(
                f"  [{i}/{total}] {r.brand}: {r.status.value} "
                f"(confidence: {r.confidence:.0%})"
            ),
        )
        print(f"\n{'=' * 60}")
        print(f"  Completed {len(results)} brands")
        print(f"{'=' * 60}\n")
        for r in results:
            _print_result(r)
            print()

    # Export CSV if requested
    if args.csv:
        SupplierResearcher.results_to_csv(results, args.csv)
        print(f"Results exported to {args.csv}")

    await browser.close()
    await pw.stop()


def _print_result(result):
    """Pretty-print a SupplierResult."""
    print(f"\n  Brand:       {result.brand}")
    print(f"  Status:      {result.status.value}")
    print(f"  Confidence:  {result.confidence:.0%}")
    if result.website:
        print(f"  Website:     {result.website}")
    if result.emails:
        print(f"  Emails:      {', '.join(result.emails)}")
    if result.phones:
        print(f"  Phones:      {', '.join(result.phones)}")
    if result.wholesale_url:
        print(f"  Wholesale:   {result.wholesale_url}")
    if result.application_url:
        print(f"  Application: {result.application_url}")
    if result.requirements:
        print(f"  Requirements: {'; '.join(result.requirements)}")
    if result.restrictions:
        print(f"  Restrictions: {'; '.join(result.restrictions)}")
    if result.notes:
        print(f"  Notes:       {result.notes}")


if __name__ == "__main__":
    asyncio.run(main())
