"""
Example: Batch queries with rate limiting.

Usage:  python examples/batch_query.py
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
from keepa_automation import KeepaAutomation


async def main():
    ka = KeepaAutomation(headless=False)
    await ka.setup(download_dir="keepa_downloads")
    await ka.login()

    # Split sellers into batches of ~20
    batch1 = ["A1GD4LR25P9QFR", "AMY4I718ZUBOU", "A2SE2RBODV3OK8"]
    batch2 = ["ATVPDKIKX0DER"]  # replace with real seller IDs

    results = await ka.search_batch(
        seller_id_batches=[batch1, batch2],
        delay_between_batches=15,
        price_min=15,
        price_max=70,
        bsr_max=100000,
    )

    # Save each batch
    for i, products in enumerate(results):
        if products:
            df = pd.DataFrame(products)
            path = f"keepa_downloads/batch_{i + 1}.csv"
            df.to_csv(path, index=False)
            print(f"Batch {i + 1}: {len(products)} products → {path}")
        else:
            print(f"Batch {i + 1}: FAILED (empty)")

    await ka.close()


if __name__ == "__main__":
    asyncio.run(main())
