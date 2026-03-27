# Playwright Engine — FBA Command Center Integration Guide

## Quick Start

```python
from keepa_automation import KeepaAutomation

async def get_seller_catalog(seller_id: str) -> list[dict]:
    ka = KeepaAutomation(headless=True)
    await ka.setup()
    await ka.login()  # reuses saved session from keepa_session.json
    catalog = await ka.seller_lookup(seller_id)
    await ka.close()
    return catalog
```

## Available Methods

### 1. Seller Storefront Lookup (~9s per seller)

Navigate to a seller's Keepa storefront and export their full product catalog.

```python
# Single seller
catalog = await ka.seller_lookup("AVFHERP2L596L")
# Returns: list[dict] — 160 columns per product

# Batch (rate-limited)
catalogs = await ka.seller_lookup_batch(
    ["AVFHERP2L596L", "A3P5ROKL5A1OLE"],
    delay_between=10,  # seconds between sellers
    max_retries=1,
)
# Returns: dict[seller_id, list[dict]]
```

### 2. Product Finder Search (~30s per query)

Search Keepa Product Finder by seller IDs + filters. Returns products matching ALL criteria.

```python
products = await ka.search(
    seller_ids=["A3P5ROKL5A1OLE"],
    price_min=15,       # Buy Box price min ($)
    price_max=70,       # Buy Box price max ($)
    bsr_max=100000,     # Sales Rank ceiling
    amazon_oos_pct_min=90,  # Amazon out-of-stock % (90-day)
)
# Returns: list[dict] — 160+ columns per product

# Batch (multiple seller groups)
results = await ka.search_batch(
    seller_id_batches=[["SELLER1"], ["SELLER2", "SELLER3"]],
    delay_between_batches=15,
    price_min=15,
)
# Returns: list[list[dict]]
```

### 3. Supplier Research (separate module)

```python
from supplier_research import SupplierResearcher

researcher = SupplierResearcher(page, use_claude=False)
result = await researcher.research_brand("Kinco", category="Tools")
# Returns: SupplierResult dataclass with emails, phones, status, etc.
```

## Setup (one-time)

```python
ka = KeepaAutomation(headless=False)  # headless=True after first login
await ka.setup()
await ka.login()  # First time: opens browser for manual Keepa login
                   # After: reuses keepa_session.json automatically
```

**First run requires manual login** — the browser opens, you log into Keepa, press Enter. Session is saved to `keepa_session.json` and reused for all future runs.

## Output Format

Seller lookup and Product Finder both return `list[dict]` with **160 columns**. Key columns:

| Column | Example | Notes |
|--------|---------|-------|
| `Title` | `"Astro Tools 78218..."` | Product title |
| `ASIN` | `"B07K8..."` | Amazon ASIN |
| `Sales Rank: Current` | `3548` | Current BSR |
| `Sales Rank: 90 days avg.` | `5142` | 90-day average BSR |
| `Sales Rank: Drops last 90 days` | `262` | Sales velocity proxy |
| `Sales Rank: Reference` | `"Tools & Home Improvement"` | Root category |
| `Sales Rank: Subcategory Sales Ranks` | `"# 8 \| Top 0.01% \| Socket Wrenches"` | Sub-category rank |
| `Buy Box: Current` | `29.99` | Current Buy Box price ($) |
| `Buy Box: 90 days avg.` | `31.50` | 90-day avg Buy Box ($) |
| `Buy Box: Buy Box Seller` | `"SellerName"` | Current BB owner |
| `Buy Box: Is FBA` | `true` | FBA or FBM |
| `Buy Box: % Amazon 90 days` | `0` | % Amazon owns BB |
| `New Offer Count: Current` | `5` | Total new offers |
| `New FBA Offer Count: Current` | `3` | FBA offer count |
| `Amazon: 90 days OOS` | `100` | Amazon OOS % (0-100) |
| `FBA Pick&Pack Fee` | `5.40` | FBA fee |
| `Referral Fee %` | `15` | Amazon referral fee % |
| `Categories: Root` | `"Tools & Home Improvement"` | Root category |
| `Categories: Sub` | `"Socket Wrenches"` | Sub category |
| `Brand` | `"Astro Tools"` | Brand name |
| `Product Codes: UPC` | `"028..."` | UPC code |
| `Package: Weight (g)` | `340` | Shipping weight |
| `Package: Dimension (cm³)` | `1200` | Package volume |
| `Bought in past month` | `500` | Monthly units sold |
| `Image` | `"https://m.media-amazon..."` | Image URLs (semicolon-separated) |

Full list: 160 columns covering pricing (Buy Box, Amazon, New, Used, eBay), ranks, fees, offers, product details, dimensions, and more.

## Integration Pattern for FBA Command Center

```python
import asyncio
import pandas as pd
from keepa_automation import KeepaAutomation


class KeepaService:
    """Wrapper for FBA Command Center to call Playwright Engine."""

    def __init__(self):
        self.ka = None

    async def start(self):
        self.ka = KeepaAutomation(headless=True)
        await self.ka.setup()
        await self.ka.login()

    async def stop(self):
        if self.ka:
            await self.ka.close()

    async def get_seller_products(self, seller_id: str) -> pd.DataFrame:
        """Get a seller's full catalog as a DataFrame."""
        products = await self.ka.seller_lookup(seller_id)
        return pd.DataFrame(products)

    async def get_multiple_sellers(self, seller_ids: list[str]) -> pd.DataFrame:
        """Get catalogs for multiple sellers, tagged with seller_id."""
        catalogs = await self.ka.seller_lookup_batch(seller_ids, delay_between=10)
        all_rows = []
        for sid, products in catalogs.items():
            for p in products:
                p["_seller_id"] = sid
            all_rows.extend(products)
        return pd.DataFrame(all_rows)

    async def search_products(self, seller_ids: list[str], **filters) -> pd.DataFrame:
        """Search Product Finder with filters."""
        products = await self.ka.search(seller_ids=seller_ids, **filters)
        return pd.DataFrame(products)


# Usage
async def main():
    svc = KeepaService()
    await svc.start()

    df = await svc.get_seller_products("AVFHERP2L596L")
    print(f"{len(df)} products, {len(df.columns)} columns")

    await svc.stop()
```

## Requirements

```
pip install playwright pandas playwright-stealth
playwright install chromium
```

Or from the repo: `pip install -r requirements.txt`

## File Map

```
Playwright-Engine-/
  keepa_automation.py      # Main module — KeepaAutomation class
  supplier_research.py     # Supplier contact research module
  requirements.txt         # Python dependencies
  setup.py                 # Package setup
  examples/
    seller_lookup.py       # CLI: python examples/seller_lookup.py SELLER_ID
    single_query.py        # CLI: Product Finder single query
    batch_query.py         # CLI: Product Finder batch query
    brand_research.py      # CLI: Supplier research example
  integration/
    FBA_INTEGRATION_GUIDE.md  # This file
    keepa_service.py          # Copy-paste wrapper for FBA Command Center
```
