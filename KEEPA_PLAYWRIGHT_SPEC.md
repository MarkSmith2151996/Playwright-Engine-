# KEEPA PLAYWRIGHT AUTOMATION — Build Spec

**Repo:** keepa-playwright (standalone, merges back to FBA Command Center later)  
**Goal:** Bulletproof Playwright automation for Keepa Product Finder that reliably extracts full result sets  
**Why this exists:** Three attempts at Keepa automation have failed due to pagination (only getting 100/4000 rows), timing (pages not loading), and environment issues (Chrome not installed). This repo solves it once, properly, in isolation.

---

## The Problem We Keep Hitting

Keepa Product Finder shows results in an ag-Grid table. The current keepa_browser.py:
1. Only extracts page 1 (100 rows) — Keepa paginates, ag-Grid API only returns visible page
2. Fails on batches 2+ because the grid doesn't load (timing/throttling)
3. Falls back to DOM scraping which also fails
4. Has no retry logic, no download-based extraction, no reliability

We need an extractor that gets ALL results (thousands of rows) reliably, every time.

---

## Architecture

```
keepa-playwright/
├── README.md              # This file
├── keepa_automation.py    # Main module — the only file other code imports
├── test_keepa.py          # End-to-end tests against live Keepa
├── requirements.txt       # playwright, etc.
└── examples/
    ├── single_query.py    # Example: one seller batch query
    └── batch_query.py     # Example: multiple batches with rate limiting
```

### Core API (keepa_automation.py)

```python
class KeepaAutomation:
    """
    Reliable Keepa Product Finder automation.
    
    Usage:
        ka = KeepaAutomation(headless=False)  # visible browser for debugging
        await ka.login()                       # handles Keepa login
        
        # Single query
        products = await ka.search(
            seller_ids=['A1GD4LR25P9QFR', 'AMY4I718ZUBOU'],
            price_min=15, price_max=70,
            bsr_max=100000,
            amazon_oos_pct_min=90,
        )
        # Returns: list of dicts, one per product, ALL results (not just page 1)
        
        # Batch queries with automatic rate limiting
        all_products = await ka.search_batch(
            seller_id_batches=[
                ['A1GD4...', 'AMY4I...', ...],  # batch 1: 20 sellers
                ['B2XYZ...', 'C3ABC...', ...],  # batch 2: 20 sellers
            ],
            delay_between_batches=15,
            price_min=15, price_max=70,
            bsr_max=100000,
        )
        # Returns: list of lists, one per batch
        
        await ka.close()
    
    # Also supports:
    await ka.search_url(url)           # navigate to pre-built Keepa URL
    await ka.export_csv(output_path)   # trigger Keepa's export and save
    await ka.get_result_count()        # how many results before extracting
```

---

## Extraction Strategy (THIS IS THE KEY PART)

The ag-Grid API scraping approach is fundamentally broken for large result sets. Keepa paginates at ~100 rows and the ag-Grid JS API only returns the current page's data.

### Primary Method: Keepa's Own Export

Keepa has an export button that downloads ALL results as CSV/Excel. This bypasses pagination entirely.

```
1. Navigate to Product Finder URL with filters
2. Wait for results to load (grid becomes visible)
3. Find the export button/dropdown (look for "Export" text, download icon, or dropdown menu)
4. Click it
5. Select CSV format if there's a choice
6. Wait for download to complete
7. Parse the downloaded CSV
```

**Finding the export button:**
- It may be a button, a dropdown, or part of a toolbar above the grid
- Search for elements containing text: "Export", "CSV", "Excel", "Download"
- Also try: buttons with download icons (SVG with arrow-down path)
- Keepa's export may require selecting "All active columns" or "Only ASIN" — select "All active columns"
- There might be a confirmation dialog — handle it

**Waiting for download:**
```python
async def wait_for_download(page, download_dir, timeout=30):
    """Wait for a file to appear in download directory."""
    import asyncio, os, time
    start = time.time()
    existing = set(os.listdir(download_dir))
    while time.time() - start < timeout:
        current = set(os.listdir(download_dir))
        new_files = current - existing
        # Check for completed downloads (not .crdownload or .tmp)
        completed = [f for f in new_files if not f.endswith(('.crdownload', '.tmp', '.part'))]
        if completed:
            return os.path.join(download_dir, completed[0])
        await asyncio.sleep(0.5)
    raise TimeoutError(f"No download completed in {timeout}s")
```

### Fallback Method: ag-Grid Full Data Extraction

If the export button can't be found or doesn't work, extract from ag-Grid — but get ALL pages, not just page 1.

```javascript
// Option A: Get all row data from ag-Grid API (ignoring pagination)
const gridApi = document.querySelector('.ag-root-wrapper').__agComponent?.gridApi 
    || document.querySelector('[ref="gridBody"]')?.closest('.ag-root-wrapper')?.__vue__?.gridApi;

// Try to get ALL data, not just visible rows
const allData = [];
gridApi.forEachNode(node => allData.push(node.data));
// forEachNode iterates ALL rows regardless of pagination

// Option B: If forEachNode doesn't work, set page size to maximum
gridApi.paginationSetPageSize(10000);  // force all on one page
await new Promise(r => setTimeout(r, 2000));  // wait for re-render
const rows = gridApi.getRenderedNodes().map(n => n.data);

// Option C: Paginate manually
const totalPages = gridApi.paginationGetTotalPages();
const allRows = [];
for (let page = 0; page < totalPages; page++) {
    gridApi.paginationGoToPage(page);
    await new Promise(r => setTimeout(r, 1000));
    gridApi.forEachNodeAfterFilterAndSort(node => allRows.push(node.data));
}
```

Try Option A first (forEachNode). If it only returns visible rows, try Option B (set page size). If that fails, try Option C (manual pagination). If all fail, fall back to the export button method.

### Last Resort: DOM Scraping with Scrolling

If ag-Grid API is not accessible at all:
```python
# Scroll through the grid, scraping visible rows each time
# This is slow but works as a last resort
all_rows = set()  # use set to deduplicate
while True:
    visible = await page.query_selector_all('.ag-row')
    for row in visible:
        cells = await row.query_selector_all('.ag-cell')
        row_data = tuple(await cell.inner_text() for cell in cells)
        all_rows.add(row_data)
    
    # Scroll down
    await page.evaluate('document.querySelector(".ag-body-viewport").scrollBy(0, 500)')
    await asyncio.sleep(0.5)
    
    # Check if we've reached the bottom (no new rows)
    new_visible = await page.query_selector_all('.ag-row')
    if len(all_rows) == prev_count:
        break
    prev_count = len(all_rows)
```

---

## Reliability Requirements

### Page Load Waiting

Don't use fixed timers. Use progressive waiting:

```python
async def wait_for_keepa_results(page, timeout=60):
    """
    Wait for Keepa Product Finder results to fully load.
    Progressive checks — don't just sleep and hope.
    """
    import asyncio, time
    start = time.time()
    
    # Phase 1: Wait for grid element to exist in DOM
    while time.time() - start < timeout:
        grid = await page.query_selector('.ag-root-wrapper')
        if grid:
            break
        await asyncio.sleep(1)
    else:
        raise TimeoutError("Grid element never appeared in DOM")
    
    # Phase 2: Wait for grid to be visible (not hidden/loading)
    while time.time() - start < timeout:
        visible = await page.evaluate('''
            () => {
                const grid = document.querySelector('.ag-root-wrapper');
                if (!grid) return false;
                const rect = grid.getBoundingClientRect();
                return rect.height > 100;  // has actual content height
            }
        ''')
        if visible:
            break
        await asyncio.sleep(1)
    else:
        raise TimeoutError("Grid never became visible")
    
    # Phase 3: Wait for data to stabilize (row count stops changing)
    prev_count = 0
    stable_ticks = 0
    while time.time() - start < timeout:
        count = await page.evaluate('''
            () => document.querySelectorAll('.ag-row').length
        ''')
        if count > 0 and count == prev_count:
            stable_ticks += 1
            if stable_ticks >= 3:  # stable for 3 checks
                break
        else:
            stable_ticks = 0
        prev_count = count
        await asyncio.sleep(1)
    
    # Phase 4: Get total result count from Keepa's UI
    result_count = await get_result_count(page)
    
    return result_count
```

### Rate Limiting Between Batches

```python
async def rate_limited_batch(batches, delay=15, max_retries=1):
    """
    Run multiple Keepa queries with rate limiting.
    """
    results = []
    for i, batch in enumerate(batches):
        print(f"--- Batch {i+1}/{len(batches)} ---")
        
        for attempt in range(max_retries + 1):
            try:
                products = await search(batch)
                results.append(products)
                print(f"  OK: {len(products)} products")
                break
            except (TimeoutError, Exception) as e:
                if attempt < max_retries:
                    wait = delay * 2  # double wait on retry
                    print(f"  Retry in {wait}s: {e}")
                    await asyncio.sleep(wait)
                    # Refresh page before retry
                    await page.reload()
                    await asyncio.sleep(5)
                else:
                    print(f"  FAILED after {max_retries} retries: {e}")
                    results.append([])  # empty result for failed batch
        
        # Wait between batches (not after last one)
        if i < len(batches) - 1:
            print(f"  Waiting {delay}s...")
            await asyncio.sleep(delay)
    
    return results
```

### Login Persistence

```python
async def login(self, storage_state_path='keepa_session.json'):
    """
    Login to Keepa. Reuse saved session if available.
    """
    import os
    
    if os.path.exists(storage_state_path):
        # Try reusing saved session
        self.context = await self.browser.new_context(
            storage_state=storage_state_path
        )
        self.page = await self.context.new_page()
        
        # Verify session is still valid
        await self.page.goto('https://keepa.com/#!finder')
        await asyncio.sleep(3)
        
        # Check if we're logged in (look for user menu or login button)
        logged_in = await self.page.evaluate('''
            () => !document.querySelector('#loginButton')
        ''')
        
        if logged_in:
            print("Reused existing Keepa session")
            return
    
    # Fresh login needed
    self.context = await self.browser.new_context()
    self.page = await self.context.new_page()
    await self.page.goto('https://keepa.com/#!finder')
    
    print("Please log in to Keepa in the browser window...")
    print("Press Enter here when done...")
    input()  # wait for manual login
    
    # Save session for next time
    await self.context.storage_state(path=storage_state_path)
    print(f"Session saved to {storage_state_path}")
```

### Download Directory Setup

```python
async def setup_browser(self, headless=False, download_dir=None):
    """Launch browser with download directory configured."""
    from playwright.async_api import async_playwright
    import os
    
    self.download_dir = download_dir or os.path.join(os.getcwd(), 'keepa_downloads')
    os.makedirs(self.download_dir, exist_ok=True)
    
    self.playwright = await async_playwright().start()
    self.browser = await self.playwright.chromium.launch(
        headless=headless,
        args=['--disable-blink-features=AutomationControlled']
    )
    self.context = await self.browser.new_context(
        accept_downloads=True,
        # Set download behavior
    )
    
    # Configure download path via CDP
    page = await self.context.new_page()
    client = await page.context.new_cdp_session(page)
    await client.send('Browser.setDownloadBehavior', {
        'behavior': 'allow',
        'downloadPath': self.download_dir,
    })
    
    self.page = page
```

---

## Keepa Product Finder URL Format

Keepa URLs encode all filters. Understanding this lets us build URLs programmatically:

```
https://keepa.com/#!finder/1-{category_id}-0-{filters}
```

The seller IDs field in the URL uses `###` separator:
```
sellerIds=A1GD4LR25P9QFR###AMY4I718ZUBOU###A2SE2RBODV3OK8
```

### Building URLs

```python
def build_keepa_url(
    seller_ids: list[str],
    price_min: float = 15,
    price_max: float = 70,
    bsr_max: int = 100000,
    amazon_oos_pct_min: int = 90,
    is_fba: bool = True,
    no_hazmat: bool = True,
    no_merch: bool = True,
    domain: int = 1,  # 1 = amazon.com
) -> str:
    """
    Build a Keepa Product Finder URL with all filters.
    
    Reverse-engineer the URL format by setting filters in the UI
    and copying the resulting URL. The URL encodes everything.
    """
    # The simplest reliable approach: navigate to base URL,
    # then inject filters via the UI rather than URL encoding
    # (Keepa's URL format is not fully documented)
    
    base = f'https://keepa.com/#!finder/{domain}'
    seller_string = '###'.join(seller_ids)
    
    return base, seller_string  # return separately, paste via UI
```

### Injecting Filters via UI (More Reliable Than URL)

```python
async def apply_filters(page, seller_ids, price_min=15, price_max=70, 
                         bsr_max=100000):
    """
    Navigate to Keepa Product Finder and set filters via the UI.
    More reliable than trying to encode everything in the URL.
    """
    await page.goto('https://keepa.com/#!finder')
    await asyncio.sleep(5)  # let page fully load
    
    # Find and fill the Seller IDs field
    seller_string = '###'.join(seller_ids)
    
    # The Seller IDs input — find by label or placeholder
    # May need to expand an "Advanced" section first
    seller_input = await page.query_selector('[placeholder*="seller" i]')
    if not seller_input:
        # Try finding by nearby label text
        seller_input = await page.evaluate('''
            () => {
                const labels = document.querySelectorAll('label, span, div');
                for (const el of labels) {
                    if (el.textContent.includes('Seller') && el.textContent.includes('ID')) {
                        const input = el.closest('.filterRow')?.querySelector('input, textarea');
                        return input;
                    }
                }
                return null;
            }
        ''')
    
    if seller_input:
        await seller_input.fill(seller_string)
    else:
        print("WARNING: Could not find Seller IDs input field")
    
    # Apply other filters similarly...
    # Price min/max, BSR max, Amazon OOS%, etc.
    
    # Click "Find Products" or equivalent button
    find_button = await page.query_selector('button:has-text("Find")')
    if find_button:
        await find_button.click()
    
    # Wait for results
    await wait_for_keepa_results(page)
```

---

## Testing

### test_keepa.py

```python
"""
End-to-end tests against LIVE Keepa.
Requires Keepa login (Data Access subscription).
Run with: python -m pytest test_keepa.py -v -s
"""

import pytest
import asyncio
import os

@pytest.fixture
async def ka():
    from keepa_automation import KeepaAutomation
    ka = KeepaAutomation(headless=False)
    await ka.setup()
    await ka.login()
    yield ka
    await ka.close()

@pytest.mark.asyncio
async def test_single_seller_query(ka):
    """Query one known seller, verify we get products back."""
    # Use a known active seller ID
    products = await ka.search(seller_ids=['A1GD4LR25P9QFR'])
    assert len(products) > 0
    assert 'asin' in products[0] or 'ASIN' in products[0]
    print(f"Got {len(products)} products from single seller")

@pytest.mark.asyncio
async def test_pagination(ka):
    """Query sellers that return 100+ results, verify we get ALL results."""
    # Use multiple sellers to ensure > 100 results
    products = await ka.search(
        seller_ids=['A1GD4LR25P9QFR', 'AMY4I718ZUBOU', 'A2SE2RBODV3OK8']
    )
    print(f"Got {len(products)} products from 3 sellers")
    # Should be well over 100 if pagination is working
    assert len(products) > 100, f"Only got {len(products)} — pagination may be broken"

@pytest.mark.asyncio
async def test_batch_with_rate_limiting(ka):
    """Run 2 batches with delay, verify both succeed."""
    batch1 = ['A1GD4LR25P9QFR']
    batch2 = ['AMY4I718ZUBOU']
    results = await ka.search_batch(
        [batch1, batch2],
        delay_between_batches=15
    )
    assert len(results) == 2
    assert len(results[0]) > 0, "Batch 1 returned no results"
    assert len(results[1]) > 0, "Batch 2 returned no results"

@pytest.mark.asyncio  
async def test_result_count_matches(ka):
    """Verify extracted row count matches Keepa's displayed count."""
    products = await ka.search(seller_ids=['A1GD4LR25P9QFR'])
    displayed_count = await ka.get_result_count()
    # Allow some slack for timing
    assert abs(len(products) - displayed_count) < 5, \
        f"Extracted {len(products)} but Keepa shows {displayed_count}"

@pytest.mark.asyncio
async def test_export_csv(ka):
    """Verify CSV export contains all columns and all rows."""
    await ka.search(seller_ids=['A1GD4LR25P9QFR'])
    csv_path = await ka.export_csv('test_export.csv')
    
    import pandas as pd
    df = pd.read_csv(csv_path)
    assert len(df) > 0
    # Should have standard Keepa columns
    assert 'ASIN' in df.columns or 'asin' in df.columns
    print(f"Exported CSV: {len(df)} rows, {len(df.columns)} columns")
    
    os.remove(csv_path)  # cleanup
```

---

## Integration Back to FBA Command Center

Once this module is tested and working, integration is simple:

```python
# In FBA Command Center's seller_network_mapper.py or run_seller_expansion.py:

from keepa_automation import KeepaAutomation

async def run_expansion_cycle(seller_batches, output_dir):
    ka = KeepaAutomation(headless=False)
    await ka.setup(download_dir=output_dir)
    await ka.login()
    
    results = await ka.search_batch(
        seller_batches,
        delay_between_batches=15,
        price_min=15,
        price_max=70,
        bsr_max=100000,
    )
    
    # Save each batch result
    for i, products in enumerate(results):
        if products:
            df = pd.DataFrame(products)
            df.to_csv(f'{output_dir}/batch_{i+1}.csv', index=False)
    
    await ka.close()
    return results
```

### What the FBA Command Center Needs From This Module

1. `search(seller_ids, **filters)` → returns ALL products (not just page 1)
2. `search_batch(batches, delay)` → runs multiple queries with rate limiting
3. `export_csv(path)` → triggers Keepa's own export, saves to path
4. `login()` → handles auth with session persistence
5. Reliability — retries, timeouts, clear error messages when Keepa throttles

That's it. The FBA Command Center handles everything after the CSV exists (Tier 1, scoring, grading, network graph).

---

## Requirements

```
# requirements.txt
playwright>=1.40.0
pandas>=2.0.0
pytest>=7.0.0
pytest-asyncio>=0.21.0
```

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
```

---

## Build Order

1. Setup: browser launch, download dir, login with session persistence
2. Navigation: go to Product Finder, apply filters, wait for results
3. Extraction PRIMARY: find and click Keepa's export button, wait for download, parse CSV
4. Extraction FALLBACK: ag-Grid forEachNode → set page size → manual pagination → DOM scroll
5. Batch support: rate limiting, retry logic, failure logging
6. Tests: run against live Keepa, verify pagination, verify export
7. Integration example: show how FBA Command Center calls this module

## Rules for Claude Code

- Use async/await throughout (Playwright is async)
- Test against LIVE Keepa after building (not mocked)
- The export button approach is PRIMARY. ag-Grid scraping is FALLBACK. Don't skip to scraping.
- If you can't find the export button, screenshot the page and describe what you see — don't guess
- If Keepa changes its UI, the fallback methods should still work
- Session persistence is required — don't force login every time
- If anything is unclear about how Keepa Product Finder works, ASK before guessing
