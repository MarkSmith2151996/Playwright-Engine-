"""
Example: Single seller query against Keepa Product Finder.

Usage:  python examples/single_query.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from keepa_automation import KeepaAutomation


async def main():
    ka = KeepaAutomation(headless=False)
    await ka.setup()
    await ka.login()

    products = await ka.search(
        seller_ids=["A1GD4LR25P9QFR", "AMY4I718ZUBOU"],
        price_min=15,
        price_max=70,
        bsr_max=100000,
        amazon_oos_pct_min=90,
    )

    print(f"\nFound {len(products)} products")
    for p in products[:5]:
        print(p)

    await ka.close()


if __name__ == "__main__":
    asyncio.run(main())
