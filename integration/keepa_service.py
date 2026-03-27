"""
Keepa Service — Drop-in wrapper for FBA Command Center.

Copy this file into your FBA project and adjust the import path.
Provides a simple async interface to all Keepa automation features.

Usage:
    svc = KeepaService()
    await svc.start()
    df = await svc.get_seller_products("AVFHERP2L596L")
    await svc.stop()
"""

import asyncio
import sys
import os

import pandas as pd

# Adjust this path to where Playwright-Engine is cloned
PLAYWRIGHT_ENGINE_PATH = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, PLAYWRIGHT_ENGINE_PATH)

from keepa_automation import KeepaAutomation


class KeepaService:
    """Async Keepa data service for FBA Command Center."""

    def __init__(self, headless: bool = True, download_dir: str | None = None):
        self.headless = headless
        self.download_dir = download_dir
        self.ka: KeepaAutomation | None = None

    async def start(self):
        """Launch browser and login (reuses saved session)."""
        self.ka = KeepaAutomation(
            headless=self.headless,
            download_dir=self.download_dir,
        )
        await self.ka.setup()
        await self.ka.login()

    async def stop(self):
        """Close browser."""
        if self.ka:
            await self.ka.close()
            self.ka = None

    # ── Seller Storefront ──────────────────────────────────────────────

    async def get_seller_products(self, seller_id: str) -> pd.DataFrame:
        """
        Get a seller's full product catalog from their Keepa storefront.

        Args:
            seller_id: Amazon Seller ID (e.g. 'AVFHERP2L596L')

        Returns:
            DataFrame with ~160 columns (Sales Rank, Buy Box, fees, etc.)
            Empty DataFrame if seller not found or has no products.
        """
        products = await self.ka.seller_lookup(seller_id)
        return pd.DataFrame(products) if products else pd.DataFrame()

    async def get_multiple_sellers(
        self,
        seller_ids: list[str],
        delay_between: int = 10,
    ) -> pd.DataFrame:
        """
        Get catalogs for multiple sellers in one call.

        Each row is tagged with '_seller_id' column.

        Args:
            seller_ids: List of Amazon Seller IDs
            delay_between: Seconds between seller lookups (rate limiting)

        Returns:
            Combined DataFrame with '_seller_id' column identifying source seller.
        """
        catalogs = await self.ka.seller_lookup_batch(
            seller_ids, delay_between=delay_between
        )
        all_rows = []
        for sid, products in catalogs.items():
            for p in products:
                p["_seller_id"] = sid
            all_rows.extend(products)
        return pd.DataFrame(all_rows) if all_rows else pd.DataFrame()

    # ── Product Finder Search ──────────────────────────────────────────

    async def search_products(
        self,
        seller_ids: list[str],
        price_min: float | None = None,
        price_max: float | None = None,
        bsr_max: int | None = None,
        amazon_oos_pct_min: int | None = None,
    ) -> pd.DataFrame:
        """
        Search Keepa Product Finder with filters.

        Args:
            seller_ids: Seller IDs to search within
            price_min: Min Buy Box price ($)
            price_max: Max Buy Box price ($)
            bsr_max: Max Sales Rank
            amazon_oos_pct_min: Min Amazon out-of-stock % (0-100)

        Returns:
            DataFrame of matching products.
        """
        products = await self.ka.search(
            seller_ids=seller_ids,
            price_min=price_min,
            price_max=price_max,
            bsr_max=bsr_max,
            amazon_oos_pct_min=amazon_oos_pct_min,
        )
        return pd.DataFrame(products) if products else pd.DataFrame()


# ── Standalone test ────────────────────────────────────────────────────

if __name__ == "__main__":
    async def _test():
        svc = KeepaService(headless=False)
        await svc.start()

        # Test: Mindconnection, LLC (83 products)
        df = await svc.get_seller_products("AVFHERP2L596L")
        print(f"Got {len(df)} products, {len(df.columns)} columns")
        if not df.empty:
            print(df[["Title", "ASIN", "Sales Rank: Current", "Buy Box: Current"]].head())

        await svc.stop()

    asyncio.run(_test())
