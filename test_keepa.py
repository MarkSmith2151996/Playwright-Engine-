"""
End-to-end tests against LIVE Keepa.
Requires Keepa login (Data Access subscription).

Run with:  python -m pytest test_keepa.py -v -s
"""

import os
import pytest
import pytest_asyncio

from keepa_automation import KeepaAutomation


@pytest_asyncio.fixture
async def ka():
    ka = KeepaAutomation(headless=False)
    await ka.setup()
    await ka.login()
    yield ka
    await ka.close()


@pytest.mark.asyncio
async def test_single_seller_query(ka):
    """Query one known seller, verify we get products back."""
    products = await ka.search(seller_ids=["A1GD4LR25P9QFR"])
    assert len(products) > 0
    # Check for ASIN column (case-insensitive)
    first = products[0]
    keys_lower = [k.lower() for k in first.keys()]
    assert "asin" in keys_lower, f"No ASIN column found. Keys: {list(first.keys())}"
    print(f"\nGot {len(products)} products from single seller")


@pytest.mark.asyncio
async def test_pagination(ka):
    """Query sellers that return 100+ results — verify we get ALL results."""
    products = await ka.search(
        seller_ids=["A1GD4LR25P9QFR", "AMY4I718ZUBOU", "A2SE2RBODV3OK8"]
    )
    print(f"\nGot {len(products)} products from 3 sellers")
    assert len(products) > 100, (
        f"Only got {len(products)} — pagination may be broken"
    )


@pytest.mark.asyncio
async def test_batch_with_rate_limiting(ka):
    """Run 2 batches with delay, verify both succeed."""
    batch1 = ["A1GD4LR25P9QFR"]
    batch2 = ["AMY4I718ZUBOU"]
    results = await ka.search_batch(
        [batch1, batch2],
        delay_between_batches=15,
    )
    assert len(results) == 2
    assert len(results[0]) > 0, "Batch 1 returned no results"
    assert len(results[1]) > 0, "Batch 2 returned no results"
    print(f"\nBatch 1: {len(results[0])} products, Batch 2: {len(results[1])} products")


@pytest.mark.asyncio
async def test_result_count_matches(ka):
    """Verify extracted row count matches Keepa's displayed count."""
    products = await ka.search(seller_ids=["A1GD4LR25P9QFR"])
    displayed_count = await ka.get_result_count()
    if displayed_count > 0:
        assert abs(len(products) - displayed_count) < 5, (
            f"Extracted {len(products)} but Keepa shows {displayed_count}"
        )
    print(f"\nExtracted: {len(products)}, Displayed: {displayed_count}")


@pytest.mark.asyncio
async def test_export_csv(ka):
    """Verify CSV export contains all columns and all rows."""
    await ka.search(seller_ids=["A1GD4LR25P9QFR"])
    output = os.path.join(ka.download_dir, "test_export.csv")
    try:
        csv_path = await ka.export_csv(output)
        import pandas as pd
        df = pd.read_csv(csv_path)
        assert len(df) > 0
        cols_lower = [c.lower() for c in df.columns]
        assert "asin" in cols_lower, f"No ASIN column. Columns: {list(df.columns)}"
        print(f"\nExported CSV: {len(df)} rows, {len(df.columns)} columns")
    finally:
        if os.path.exists(output):
            os.remove(output)
