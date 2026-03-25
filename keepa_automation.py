"""
Keepa Product Finder — Playwright Automation

Bulletproof extraction of full result sets from Keepa Product Finder.
Primary method: Keepa's own CSV export (bypasses pagination).
Fallback: ag-Grid API extraction with full pagination support.
Last resort: DOM scraping with scroll.

Usage:
    ka = KeepaAutomation(headless=False)
    await ka.setup()
    await ka.login()
    products = await ka.search(seller_ids=['A1GD4LR25P9QFR'])
    await ka.close()
"""

import asyncio
import csv
import io
import logging
import os
import re
import time
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright, Page, BrowserContext, Browser
from playwright_stealth import Stealth

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class KeepaAutomation:
    """Reliable Keepa Product Finder automation with full result extraction."""

    KEEPA_FINDER_URL = "https://keepa.com/#!finder"

    def __init__(self, headless: bool = False, download_dir: str | None = None):
        self.headless = headless
        self.download_dir = download_dir or os.path.join(os.getcwd(), "keepa_downloads")
        self.playwright = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None

    # ------------------------------------------------------------------ #
    #  1. BROWSER SETUP                                                   #
    # ------------------------------------------------------------------ #

    async def setup(self, download_dir: str | None = None):
        """Launch Chromium with download directory configured."""
        if download_dir:
            self.download_dir = download_dir
        os.makedirs(self.download_dir, exist_ok=True)

        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        logger.info("Browser launched (headless=%s)", self.headless)

    async def _new_context(self, storage_state: str | None = None):
        """Create a new browser context with download support."""
        opts = {"accept_downloads": True}
        if storage_state and os.path.exists(storage_state):
            opts["storage_state"] = storage_state

        self.context = await self.browser.new_context(**opts)
        self.page = await self.context.new_page()

        # Anti-detection: make browser appear non-automated
        stealth = Stealth()
        await stealth.apply_stealth_async(self.page)

        # Configure download path via CDP
        client = await self.page.context.new_cdp_session(self.page)
        await client.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": self.download_dir},
        )

    # ------------------------------------------------------------------ #
    #  2. LOGIN WITH SESSION PERSISTENCE                                  #
    # ------------------------------------------------------------------ #

    async def login(self, storage_state_path: str = "keepa_session.json"):
        """
        Login to Keepa. Reuses a saved session if it's still valid,
        otherwise opens a browser for manual login and saves the session.
        """
        # Try reusing saved session
        if os.path.exists(storage_state_path):
            await self._new_context(storage_state=storage_state_path)
            await self.page.goto(self.KEEPA_FINDER_URL, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            logged_in = await self.page.evaluate(
                "() => !document.querySelector('#loginButton')"
            )
            if logged_in:
                logger.info("Reused existing Keepa session from %s", storage_state_path)
                return

            # Session expired — close this context
            logger.info("Saved session expired, need fresh login")
            await self.context.close()

        # Fresh login
        await self._new_context()
        await self.page.goto(self.KEEPA_FINDER_URL, wait_until="domcontentloaded")

        print("\n" + "=" * 60)
        print("  Please log in to Keepa in the browser window.")
        print("  Press ENTER here when you're done.")
        print("=" * 60 + "\n")
        await asyncio.get_event_loop().run_in_executor(None, input)

        # Save session
        await self.context.storage_state(path=storage_state_path)
        logger.info("Session saved to %s", storage_state_path)

    # ------------------------------------------------------------------ #
    #  3. NAVIGATION & FILTERS                                            #
    # ------------------------------------------------------------------ #

    async def _wait_for_results(self, timeout: int = 60) -> int:
        """
        Progressive wait for Keepa Product Finder results to fully load.
        Returns the result count shown by Keepa.
        """
        page = self.page
        start = time.time()

        # Phase 1: grid element in DOM
        while time.time() - start < timeout:
            grid = await page.query_selector(".ag-root-wrapper")
            if grid:
                break
            await asyncio.sleep(1)
        else:
            raise TimeoutError("Grid element never appeared in DOM")

        # Phase 2: grid has real height (not collapsed / loading)
        while time.time() - start < timeout:
            visible = await page.evaluate("""
                () => {
                    const g = document.querySelector('.ag-root-wrapper');
                    return g ? g.getBoundingClientRect().height > 100 : false;
                }
            """)
            if visible:
                break
            await asyncio.sleep(1)
        else:
            raise TimeoutError("Grid never became visible")

        # Check for "No results found" dialog
        no_results = await page.query_selector('text="No results found"')
        if no_results:
            # Dismiss the dialog
            close_btn = await page.query_selector('button:has-text("OK"), .close, [class*="close"]')
            if close_btn:
                await close_btn.click()
            logger.warning("Keepa returned no results for this query")
            return 0

        # Phase 3: row count stabilises
        prev_count = 0
        stable_ticks = 0
        while time.time() - start < timeout:
            # Also check for late "no results" dialog
            no_results = await page.query_selector('text="No results found"')
            if no_results:
                logger.warning("Keepa returned no results for this query")
                return 0

            count = await page.evaluate(
                "() => document.querySelectorAll('.ag-row').length"
            )
            if count > 0 and count == prev_count:
                stable_ticks += 1
                if stable_ticks >= 3:
                    break
            else:
                stable_ticks = 0
            prev_count = count
            await asyncio.sleep(1)

        result_count = await self.get_result_count()
        logger.info("Results loaded — grid shows %s rows, Keepa header says %s", prev_count, result_count)
        return result_count

    async def get_result_count(self) -> int:
        """Read the total result count displayed by Keepa's UI.

        Keepa toolbar shows: "Number of results: 100 (out of a total result of 20,304)"
        We want the TOTAL number (20,304), not the displayed page count (100).
        """
        count = await self.page.evaluate("""
            () => {
                const all = document.querySelectorAll('span, div, p');
                for (const el of all) {
                    const t = el.textContent;
                    // Match "total result of X" pattern
                    const totalMatch = t.match(/total\\s+result\\s+of\\s+([\\d,]+)/i);
                    if (totalMatch) return parseInt(totalMatch[1].replace(/,/g, ''), 10);
                }
                // Fallback: "Number of results: X"
                for (const el of all) {
                    const t = el.textContent;
                    const numMatch = t.match(/Number\\s+of\\s+results:\\s*([\\d,]+)/i);
                    if (numMatch) return parseInt(numMatch[1].replace(/,/g, ''), 10);
                }
                // Last resort: ag-Grid paging summary "X to Y of Z"
                const paging = document.querySelector('.ag-paging-row-summary-panel');
                if (paging) {
                    const m = paging.textContent.match(/of\\s+([\\d,]+)/);
                    if (m) return parseInt(m[1].replace(/,/g, ''), 10);
                }
                return -1;
            }
        """)
        return count

    async def apply_filters(
        self,
        seller_ids: list[str],
        price_min: float | None = None,
        price_max: float | None = None,
        bsr_max: int | None = None,
        amazon_oos_pct_min: int | None = None,
    ):
        """Navigate to Product Finder and apply filters via the UI.

        Uses exact Keepa element IDs discovered from the live DOM:
          - Seller IDs:   #textArray-sellerIds  (comma-separated)
          - Buy Box price: #numberFrom-BUY_BOX_SHIPPING_current / #numberTo-BUY_BOX_SHIPPING_current
          - Sales Rank:    #numberFrom-SALES_current / #numberTo-SALES_current
          - Amazon OOS %:  #numberFrom-AMAZON_outOfStockPercentage90
        """
        page = self.page
        await page.goto(self.KEEPA_FINDER_URL, wait_until="domcontentloaded")
        # Wait for the filter form to be ready
        await page.wait_for_selector("#textArray-sellerIds", timeout=15000)
        await asyncio.sleep(1)

        # --- Helper to fill an input by ID ---
        async def _fill_by_id(element_id: str, value) -> bool:
            el = await page.query_selector(f"#{element_id}")
            if el:
                await el.click()
                await el.fill(str(value))
                return True
            logger.warning("Element #%s not found", element_id)
            return False

        # --- Seller IDs ---
        # Keepa accepts comma-separated seller IDs in #textArray-sellerIds
        seller_string = ",".join(seller_ids)
        if await _fill_by_id("textArray-sellerIds", seller_string):
            logger.info("Filled seller IDs (%d sellers)", len(seller_ids))
        else:
            logger.warning("Could not find Seller IDs field — taking screenshot")
            await page.screenshot(path=os.path.join(self.download_dir, "debug_no_seller_field.png"))

        # --- Buy Box Price (min / max) — Keepa uses dollar values ---
        if price_min is not None:
            await _fill_by_id("numberFrom-BUY_BOX_SHIPPING_current", int(price_min))
        if price_max is not None:
            await _fill_by_id("numberTo-BUY_BOX_SHIPPING_current", int(price_max))

        # --- Sales Rank / BSR (max) ---
        if bsr_max is not None:
            await _fill_by_id("numberTo-SALES_current", bsr_max)

        # --- Amazon Out-of-Stock % 90-day (min) ---
        if amazon_oos_pct_min is not None:
            await _fill_by_id("numberFrom-AMAZON_outOfStockPercentage90", amazon_oos_pct_min)

        # --- Click "Find Products" ---
        # The button is inside a blue banner area near the top
        find_btn = await page.query_selector(
            '#findProductsButton, button:has-text("Find Products"), '
            'a:has-text("Find Products"), button:has-text("FIND PRODUCTS")'
        )
        if not find_btn:
            # Broader search — any button/link with "Find" near the top of the page
            find_btn = await page.evaluate_handle("""
                () => {
                    const btns = document.querySelectorAll('button, a, [role="button"]');
                    for (const btn of btns) {
                        const text = btn.textContent.trim();
                        if (/find\\s*products/i.test(text)) return btn;
                    }
                    // fallback: any element with "Find" in a prominent position
                    for (const btn of btns) {
                        if (/^find$/i.test(btn.textContent.trim())) return btn;
                    }
                    return null;
                }
            """)

        if find_btn:
            await find_btn.click()
            logger.info("Clicked Find Products")
        else:
            logger.warning("Could not locate Find button — taking screenshot")
            await page.screenshot(path=os.path.join(self.download_dir, "debug_no_find_btn.png"))

        await self._wait_for_results()

    # ------------------------------------------------------------------ #
    #  4. EXTRACTION — PRIMARY: KEEPA EXPORT                              #
    # ------------------------------------------------------------------ #

    async def _wait_for_download(self, timeout: int = 60) -> str:
        """Wait for a new file to appear in the download directory."""
        existing = set(os.listdir(self.download_dir))
        start = time.time()
        while time.time() - start < timeout:
            current = set(os.listdir(self.download_dir))
            new_files = current - existing
            completed = [
                f for f in new_files
                if not f.endswith((".crdownload", ".tmp", ".part"))
            ]
            if completed:
                path = os.path.join(self.download_dir, completed[0])
                logger.info("Download complete: %s", path)
                return path
            await asyncio.sleep(0.5)
        raise TimeoutError(f"No download completed in {timeout}s")

    async def _download_via_playwright(self, click_coro) -> str:
        """Use Playwright's download event to capture a file.

        click_coro should be an awaitable that triggers the download
        (e.g. clicking the EXPORT button).
        Returns the saved file path.
        """
        async with self.page.expect_download(timeout=90000) as download_info:
            await click_coro
        download = await download_info.value
        save_path = os.path.join(self.download_dir, download.suggested_filename)
        await download.save_as(save_path)
        logger.info("Download saved: %s", save_path)
        return save_path

    async def _set_page_size_max(self):
        """
        Change the grid's "100 rows" page size to 5000 (the maximum).

        Keepa toolbar structure:
          <span class="tool__row mdc-menu-anchor">
            <span class="trigger">"100 rows"</span>   ← click this
            <div class="mdc-menu">                     ← dropdown appears
              <li>5</li><li>20</li>...<li>5000</li>    ← click 5000
            </div>
          </span>
        """
        page = self.page

        # Click the "rows" trigger in the toolbar
        trigger = await page.query_selector(".tool__row .trigger")
        if not trigger:
            logger.warning("Could not find rows-per-page trigger (.tool__row .trigger)")
            return False

        await trigger.click()
        await asyncio.sleep(1)

        # The MDC menu is now open — find and click "5000"
        selected = await page.evaluate("""
            () => {
                // MDC menu items inside .tool__row
                const menu = document.querySelector('.tool__row .mdc-menu');
                if (!menu) return 0;
                const items = menu.querySelectorAll('li, .mdc-list-item');
                for (const item of items) {
                    const text = item.textContent.trim();
                    if (text === '5000') {
                        item.click();
                        return 5000;
                    }
                }
                // Fallback: click the last (largest) option
                const allItems = [...items].filter(i => /^\\d+$/.test(i.textContent.trim()));
                if (allItems.length > 0) {
                    const last = allItems[allItems.length - 1];
                    last.click();
                    return parseInt(last.textContent.trim(), 10);
                }
                return 0;
            }
        """)

        if selected > 0:
            logger.info("Set page size to %d rows", selected)
            # Wait for the toolbar "Number of results" to update.
            # When it shows more than 100 (e.g. "2,805"), the grid has reloaded.
            # ag-Grid virtualizes so visible .ag-row count stays ~30 regardless.
            for i in range(30):
                result_text = await page.evaluate("""
                    () => {
                        const el = document.querySelector('.tool__results');
                        return el ? el.textContent : '';
                    }
                """)
                # "Number of results: 2,805 (out of a total result of 2,805)"
                m = re.search(r'Number of results:\s*([\d,]+)', result_text)
                if m:
                    displayed = int(m.group(1).replace(',', ''))
                    if displayed > 100:
                        logger.info("Grid loaded — showing %d results", displayed)
                        break
                await asyncio.sleep(1)
            await asyncio.sleep(2)
            return True
        else:
            logger.warning("Could not select a larger page size")
            return False

    async def _extract_via_export(self) -> list[dict] | None:
        """
        PRIMARY extraction: maximize page size, then click Keepa's Export.

        IMPORTANT: Keepa's export only exports the currently displayed page,
        so we first set page size to maximum, then export.
        """
        page = self.page
        logger.info("Attempting extraction via Keepa Export...")

        # Step 1: Maximize page size so export gets all rows
        await self._set_page_size_max()

        # Step 2: Click Export
        export_el = await page.evaluate_handle("""
            () => {
                const candidates = document.querySelectorAll(
                    'span, a, button, div[role="button"]'
                );
                for (const el of candidates) {
                    const text = el.textContent.trim();
                    if (/^\\s*Export\\s*$/i.test(text)) {
                        const rect = el.getBoundingClientRect();
                        if (rect.height > 0 && rect.width > 0) return el;
                    }
                }
                return null;
            }
        """)

        is_null = await page.evaluate("el => el === null", export_el)
        if is_null:
            logger.warning("Export link not found — will try fallback")
            await page.screenshot(path=os.path.join(self.download_dir, "debug_no_export_btn.png"))
            return None

        await export_el.click()
        logger.info("Clicked Export")
        await asyncio.sleep(2)

        # Step 3: In the dialog, ensure "All active columns" and "CSV" are selected
        # "All active columns" radio
        all_cols = await page.query_selector('text="All active columns"')
        if all_cols:
            await all_cols.click()
            await asyncio.sleep(0.3)

        # "CSV" radio
        csv_radio = await page.query_selector('text="CSV"')
        if csv_radio:
            await csv_radio.click()
            await asyncio.sleep(0.3)

        # Step 4: Click the blue "EXPORT" button in the dialog
        export_btn = await page.evaluate_handle("""
            () => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (/^\\s*EXPORT\\s*$/i.test(btn.textContent.trim())) {
                        return btn;
                    }
                }
                return null;
            }
        """)
        is_null = await page.evaluate("el => el === null", export_btn)
        if is_null:
            logger.warning("EXPORT button not found in dialog")
            await page.screenshot(path=os.path.join(self.download_dir, "debug_export_dialog.png"))
            return None

        # Step 5: Click EXPORT and capture the download via Playwright
        try:
            csv_path = await self._download_via_playwright(export_btn.click())
        except Exception as e:
            logger.warning("Playwright download failed: %s — trying directory poll", e)
            # Fallback to directory polling (file may already be there)
            try:
                csv_path = await self._wait_for_download(timeout=30)
            except TimeoutError:
                logger.warning("Export download timed out")
                return None

        return self._parse_downloaded_file(csv_path)

    def _parse_downloaded_file(self, path: str) -> list[dict]:
        """Parse a CSV or Excel file downloaded from Keepa."""
        ext = Path(path).suffix.lower()
        if ext in (".xlsx", ".xls"):
            df = pd.read_excel(path)
        elif ext == ".csv":
            df = pd.read_csv(path)
        else:
            # Try CSV first, then Excel
            try:
                df = pd.read_csv(path)
            except Exception:
                df = pd.read_excel(path)

        logger.info("Parsed %d rows, %d columns from %s", len(df), len(df.columns), path)
        return df.to_dict("records")

    # ------------------------------------------------------------------ #
    #  5. EXTRACTION — FALLBACK: AG-GRID API                              #
    # ------------------------------------------------------------------ #

    async def _extract_via_ag_grid(self) -> list[dict] | None:
        """
        FALLBACK extraction: use ag-Grid's JavaScript API.
        Tries forEachNode → set page size → manual pagination.
        """
        page = self.page
        logger.info("Attempting extraction via ag-Grid API...")

        # Option A: forEachNode (gets ALL rows regardless of pagination)
        rows = await page.evaluate("""
            () => {
                try {
                    const wrapper = document.querySelector('.ag-root-wrapper');
                    if (!wrapper) return null;

                    // Try multiple ways to find gridApi
                    let gridApi = null;
                    if (wrapper.__agComponent && wrapper.__agComponent.gridApi) {
                        gridApi = wrapper.__agComponent.gridApi;
                    } else if (wrapper.__vue__ && wrapper.__vue__.gridApi) {
                        gridApi = wrapper.__vue__.gridApi;
                    } else {
                        // Search for gridOptions on nearby elements
                        const root = wrapper.querySelector('.ag-root');
                        if (root && root.__agComponent) {
                            gridApi = root.__agComponent.gridApi;
                        }
                    }
                    if (!gridApi) return null;

                    const allData = [];
                    gridApi.forEachNode(node => {
                        if (node.data) allData.push(node.data);
                    });
                    return allData.length > 0 ? allData : null;
                } catch (e) {
                    return null;
                }
            }
        """)

        if rows and len(rows) > 0:
            logger.info("ag-Grid forEachNode returned %d rows", len(rows))
            return rows

        # Option B: force page size to 10000
        logger.info("forEachNode failed — trying page size override...")
        rows = await page.evaluate("""
            async () => {
                try {
                    const wrapper = document.querySelector('.ag-root-wrapper');
                    let gridApi = wrapper?.__agComponent?.gridApi
                                || wrapper?.__vue__?.gridApi;
                    if (!gridApi) return null;

                    gridApi.paginationSetPageSize(10000);
                    await new Promise(r => setTimeout(r, 2000));

                    const allData = [];
                    gridApi.forEachNode(node => {
                        if (node.data) allData.push(node.data);
                    });
                    return allData.length > 0 ? allData : null;
                } catch (e) {
                    return null;
                }
            }
        """)

        if rows and len(rows) > 0:
            logger.info("ag-Grid page-size override returned %d rows", len(rows))
            return rows

        # Option C: manual pagination
        logger.info("Page size override failed — trying manual pagination...")
        rows = await page.evaluate("""
            async () => {
                try {
                    const wrapper = document.querySelector('.ag-root-wrapper');
                    let gridApi = wrapper?.__agComponent?.gridApi
                                || wrapper?.__vue__?.gridApi;
                    if (!gridApi) return null;

                    const totalPages = gridApi.paginationGetTotalPages();
                    if (!totalPages || totalPages <= 0) return null;

                    const allRows = [];
                    const seen = new Set();
                    for (let p = 0; p < totalPages; p++) {
                        gridApi.paginationGoToPage(p);
                        await new Promise(r => setTimeout(r, 1000));
                        gridApi.forEachNodeAfterFilterAndSort(node => {
                            if (node.data) {
                                const key = JSON.stringify(node.data);
                                if (!seen.has(key)) {
                                    seen.add(key);
                                    allRows.push(node.data);
                                }
                            }
                        });
                    }
                    return allRows.length > 0 ? allRows : null;
                } catch (e) {
                    return null;
                }
            }
        """)

        if rows and len(rows) > 0:
            logger.info("ag-Grid manual pagination returned %d rows", len(rows))
            return rows

        logger.warning("All ag-Grid extraction methods failed")
        return None

    # ------------------------------------------------------------------ #
    #  6. EXTRACTION — LAST RESORT: DOM SCROLL SCRAPING                   #
    # ------------------------------------------------------------------ #

    async def _extract_via_dom_scroll(self) -> list[dict] | None:
        """
        LAST RESORT: scroll through the grid and scrape visible rows.
        Slow but works when ag-Grid API is inaccessible.
        """
        page = self.page
        logger.info("Attempting extraction via DOM scroll scraping (slow)...")

        # Get column headers first
        headers = await page.evaluate("""
            () => {
                const cells = document.querySelectorAll('.ag-header-cell-text');
                return Array.from(cells).map(c => c.textContent.trim());
            }
        """)
        if not headers:
            logger.warning("Could not extract grid headers")
            return None

        all_rows = {}
        prev_count = 0
        stale_rounds = 0

        for scroll_round in range(500):  # safety cap
            visible = await page.evaluate("""
                () => {
                    const rows = document.querySelectorAll('.ag-row');
                    return Array.from(rows).map(row => {
                        const cells = row.querySelectorAll('.ag-cell');
                        return {
                            id: row.getAttribute('row-id') || row.getAttribute('row-index'),
                            data: Array.from(cells).map(c => c.textContent.trim())
                        };
                    });
                }
            """)

            for r in visible:
                rid = r["id"]
                if rid and rid not in all_rows:
                    all_rows[rid] = r["data"]

            if len(all_rows) == prev_count:
                stale_rounds += 1
                if stale_rounds >= 5:
                    break
            else:
                stale_rounds = 0
            prev_count = len(all_rows)

            # Scroll the grid viewport down
            await page.evaluate(
                'document.querySelector(".ag-body-viewport")?.scrollBy(0, 500)'
            )
            await asyncio.sleep(0.4)

        if not all_rows:
            return None

        # Map to list of dicts using headers
        result = []
        for row_data in all_rows.values():
            record = {}
            for i, val in enumerate(row_data):
                key = headers[i] if i < len(headers) else f"col_{i}"
                record[key] = val
            result.append(record)

        logger.info("DOM scroll scraping extracted %d rows", len(result))
        return result

    # ------------------------------------------------------------------ #
    #  7. UNIFIED EXTRACT — tries all methods in order                    #
    # ------------------------------------------------------------------ #

    async def _extract_all(self) -> list[dict]:
        """Run extraction strategy: Export → ag-Grid → DOM scroll."""
        # Primary: Keepa export
        data = await self._extract_via_export()
        if data:
            return data

        # Fallback: ag-Grid API
        data = await self._extract_via_ag_grid()
        if data:
            return data

        # Last resort: DOM scroll
        data = await self._extract_via_dom_scroll()
        if data:
            return data

        raise RuntimeError(
            "All extraction methods failed. "
            "Check debug screenshots in: " + self.download_dir
        )

    # ------------------------------------------------------------------ #
    #  8. PUBLIC API: search, search_batch, search_url, export_csv        #
    # ------------------------------------------------------------------ #

    async def search(
        self,
        seller_ids: list[str],
        price_min: float | None = None,
        price_max: float | None = None,
        bsr_max: int | None = None,
        amazon_oos_pct_min: int | None = None,
    ) -> list[dict]:
        """
        Query Keepa Product Finder for the given sellers/filters
        and return ALL matching products as a list of dicts.
        """
        await self.apply_filters(
            seller_ids=seller_ids,
            price_min=price_min,
            price_max=price_max,
            bsr_max=bsr_max,
            amazon_oos_pct_min=amazon_oos_pct_min,
        )
        return await self._extract_all()

    async def search_url(self, url: str) -> list[dict]:
        """Navigate to a pre-built Keepa Product Finder URL and extract results."""
        await self.page.goto(url, wait_until="domcontentloaded")
        await self._wait_for_results()
        return await self._extract_all()

    async def search_batch(
        self,
        seller_id_batches: list[list[str]],
        delay_between_batches: int = 15,
        max_retries: int = 1,
        price_min: float | None = None,
        price_max: float | None = None,
        bsr_max: int | None = None,
        amazon_oos_pct_min: int | None = None,
    ) -> list[list[dict]]:
        """
        Run multiple Keepa queries with rate limiting between batches.
        Returns a list of result lists, one per batch.
        Failed batches return an empty list.
        """
        results = []

        for i, batch in enumerate(seller_id_batches):
            logger.info("--- Batch %d/%d (%d sellers) ---", i + 1, len(seller_id_batches), len(batch))

            for attempt in range(max_retries + 1):
                try:
                    products = await self.search(
                        seller_ids=batch,
                        price_min=price_min,
                        price_max=price_max,
                        bsr_max=bsr_max,
                        amazon_oos_pct_min=amazon_oos_pct_min,
                    )
                    results.append(products)
                    logger.info("  Batch %d OK: %d products", i + 1, len(products))
                    break
                except Exception as e:
                    if attempt < max_retries:
                        wait = delay_between_batches * 2
                        logger.warning("  Batch %d attempt %d failed: %s — retrying in %ds", i + 1, attempt + 1, e, wait)
                        await asyncio.sleep(wait)
                        await self.page.reload(wait_until="domcontentloaded")
                        await asyncio.sleep(5)
                    else:
                        logger.error("  Batch %d FAILED after %d retries: %s", i + 1, max_retries, e)
                        results.append([])

            # Delay between batches (skip after last)
            if i < len(seller_id_batches) - 1:
                logger.info("  Waiting %ds before next batch...", delay_between_batches)
                await asyncio.sleep(delay_between_batches)

        return results

    async def export_csv(self, output_path: str) -> str:
        """
        Trigger Keepa's export and save the result to output_path.
        Assumes results are already loaded on the page.
        Returns the path to the saved file.
        """
        csv_path = await self._wait_for_download_after_export()
        # Copy/move to requested output path
        import shutil
        shutil.move(csv_path, output_path)
        logger.info("Exported to %s", output_path)
        return output_path

    async def _wait_for_download_after_export(self) -> str:
        """Click export and wait for the file. Reuses _extract_via_export logic."""
        page = self.page

        export_el = await page.evaluate_handle("""
            () => {
                const candidates = document.querySelectorAll('span, a, button, div[role="button"]');
                for (const el of candidates) {
                    if (/^\\s*Export\\s*$/i.test(el.textContent.trim())) {
                        const rect = el.getBoundingClientRect();
                        if (rect.height > 0 && rect.width > 0) return el;
                    }
                }
                return null;
            }
        """)
        is_null = await page.evaluate("el => el === null", export_el)
        if is_null:
            raise RuntimeError("Export link not found on current page")

        await export_el.click()
        await asyncio.sleep(2)

        # Handle dialogs
        await asyncio.sleep(2)
        csv_radio = await page.query_selector('text="CSV"')
        if csv_radio:
            await csv_radio.click()
            await asyncio.sleep(0.3)

        all_cols = await page.query_selector('text="All active columns"')
        if all_cols:
            await all_cols.click()
            await asyncio.sleep(0.3)

        # Find and click the EXPORT button in dialog, capturing download
        export_confirm = await page.evaluate_handle("""
            () => {
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (/^\\s*EXPORT\\s*$/i.test(btn.textContent.trim())) return btn;
                }
                return null;
            }
        """)
        return await self._download_via_playwright(export_confirm.click())

    # ------------------------------------------------------------------ #
    #  9. CLEANUP                                                         #
    # ------------------------------------------------------------------ #

    async def close(self):
        """Close browser and Playwright."""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        logger.info("Browser closed")
