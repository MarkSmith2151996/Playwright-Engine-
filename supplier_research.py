"""
Supplier Contact Research Engine

Given a brand name and optional category, searches Google for wholesale/dealer
pages, visits the top results, and extracts structured contact info (email,
phone, application URL, requirements, restrictions).

Two-pass approach:
  1. Playwright + regex (free, ~70-80% hit rate)
  2. Optional Claude CLI fallback (~$0.001/brand for the remaining 20-30%)

Usage:
    researcher = SupplierResearcher(page)
    result = await researcher.research("Kinco", category="Tools")
    print(result)

The caller is responsible for browser setup and stealth — this module
only needs a working Playwright Page object.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from urllib.parse import quote_plus

from ddgs import DDGS
from playwright.async_api import Page

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
#  DATA TYPES                                                         #
# ------------------------------------------------------------------ #

class SupplierStatus(str, Enum):
    WHOLESALE_DIRECT = "WHOLESALE_DIRECT"
    APPLICATION_REQUIRED = "APPLICATION_REQUIRED"
    DISTRIBUTOR_ONLY = "DISTRIBUTOR_ONLY"
    RETAIL_ONLY = "RETAIL_ONLY"
    UNKNOWN = "UNKNOWN"


@dataclass
class SupplierResult:
    brand: str
    status: SupplierStatus = SupplierStatus.UNKNOWN
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    website: str = ""
    wholesale_url: str = ""
    application_url: str = ""
    requirements: list[str] = field(default_factory=list)
    restrictions: list[str] = field(default_factory=list)
    min_order: str = ""
    notes: str = ""
    source_urls: list[str] = field(default_factory=list)
    confidence: float = 0.0
    method: str = "playwright"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


# ------------------------------------------------------------------ #
#  CONFIGURATION                                                      #
# ------------------------------------------------------------------ #

# Domains that never contain useful supplier contact info
BLACKLISTED_DOMAINS = {
    "amazon.com", "amazon.co.uk", "amazon.ca",
    "ebay.com", "ebay.co.uk",
    "walmart.com", "target.com",
    "alibaba.com", "aliexpress.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "pinterest.com",
    "reddit.com", "quora.com",
    "linkedin.com",
    "wikipedia.org",
    "yelp.com",
    "bbb.org",
    "glassdoor.com",
    "indeed.com",
}

# URL patterns that boost relevance score
WHOLESALE_URL_KEYWORDS = [
    "wholesale", "dealer", "distributor", "reseller", "b2b",
    "trade", "commercial", "pro", "professional",
    "apply", "application", "account", "register",
    "become-a-dealer", "dealer-locator", "where-to-buy",
    "contact", "contact-us",
]

# Page content keywords that indicate wholesale/dealer programs
WHOLESALE_CONTENT_KEYWORDS = [
    "wholesale", "dealer program", "dealer application",
    "become a dealer", "become a distributor", "reseller program",
    "trade account", "commercial account", "pro account",
    "minimum order", "MOQ", "net 30", "net terms",
    "authorized dealer", "authorized distributor",
    "wholesale pricing", "dealer pricing", "volume discount",
    "apply now", "open an account", "dealer inquiry",
]

# Regex patterns for extraction
EMAIL_PATTERN = re.compile(
    r'\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b'
)
PHONE_PATTERN = re.compile(
    r'(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'
)

# Emails to ignore (generic/spam traps)
JUNK_EMAIL_DOMAINS = {
    "example.com", "test.com", "sentry.io", "wixpress.com",
    "googleapis.com", "googleusercontent.com",
}

# Max pages to visit per brand
MAX_PAGES_PER_BRAND = 3

# Google search delay (seconds)
SEARCH_DELAY = 2.0

# Brand search delay between batch items (seconds)
BATCH_DELAY = 8.0


# ------------------------------------------------------------------ #
#  MAIN CLASS                                                         #
# ------------------------------------------------------------------ #

class SupplierResearcher:
    """
    Research supplier/wholesale contact info for brands.

    Args:
        page: An existing Playwright Page object (caller manages browser).
        use_claude: If True, use Claude CLI as fallback for ambiguous results.
        screenshot_dir: Directory for debug screenshots (None = disabled).
    """

    def __init__(
        self,
        page: Page,
        use_claude: bool = False,
        screenshot_dir: str | None = None,
    ):
        self.page = page
        self.use_claude = use_claude
        self.screenshot_dir = screenshot_dir
        if screenshot_dir:
            os.makedirs(screenshot_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    #  SEARCH (Google → DuckDuckGo fallback)                              #
    # ------------------------------------------------------------------ #

    async def _search(self, brand: str, category: str = "") -> list[dict]:
        """
        Search for brand wholesale/dealer pages.
        Tries Google first, falls back to DuckDuckGo on CAPTCHA or failure.
        Returns list of {url, title, snippet} dicts.
        """
        results = await self._google_search(brand, category)
        if results:
            return results

        logger.info("Falling back to DuckDuckGo for '%s'", brand)
        return await self._duckduckgo_search(brand, category)

    async def _google_search(self, brand: str, category: str = "") -> list[dict]:
        """
        Google for brand wholesale/dealer pages.
        Returns list of {url, title, snippet} dicts, or [] on CAPTCHA/failure.
        """
        query_parts = [brand]
        if category:
            query_parts.append(category)
        query_parts.append("wholesale dealer contact")
        query = " ".join(query_parts)

        url = f"https://www.google.com/search?q={quote_plus(query)}&num=10"

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(SEARCH_DELAY)
        except Exception as e:
            logger.warning("Google navigation failed: %s", e)
            return []

        # Check for CAPTCHA
        content = await self.page.content()
        if "captcha" in content.lower() or "unusual traffic" in content.lower():
            logger.warning("Google CAPTCHA detected for '%s'", brand)
            if self.screenshot_dir:
                await self.page.screenshot(
                    path=os.path.join(self.screenshot_dir, f"captcha_{brand}.png")
                )
            return []

        # Extract search results
        results = await self.page.evaluate("""
            () => {
                const items = [];
                const containers = document.querySelectorAll('#search .g, #rso .g');
                for (const g of containers) {
                    const linkEl = g.querySelector('a[href]');
                    const titleEl = g.querySelector('h3');
                    const snippetEl = g.querySelector('[data-sncf], .VwiC3b, [style*="-webkit-line-clamp"]');
                    if (linkEl && titleEl) {
                        const href = linkEl.getAttribute('href');
                        if (href && href.startsWith('http')) {
                            items.push({
                                url: href,
                                title: titleEl.textContent.trim(),
                                snippet: snippetEl ? snippetEl.textContent.trim() : '',
                            });
                        }
                    }
                }
                return items;
            }
        """)

        return self._filter_blacklisted(results, "Google", brand)

    async def _duckduckgo_search(self, brand: str, category: str = "") -> list[dict]:
        """
        Search DuckDuckGo via API library (no browser needed, no CAPTCHA).
        Returns list of {url, title, snippet} dicts.
        """
        query_parts = [brand]
        if category:
            query_parts.append(category)
        query_parts.append("wholesale dealer contact")
        query = " ".join(query_parts)

        try:
            # Run synchronous DDGS in a thread to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            raw = await loop.run_in_executor(
                None, lambda: list(DDGS().text(query, max_results=10))
            )
        except Exception as e:
            logger.warning("DuckDuckGo API search failed: %s", e)
            return []

        # Normalize to our standard format {url, title, snippet}
        results = []
        for r in raw:
            results.append({
                "url": r.get("href", ""),
                "title": r.get("title", ""),
                "snippet": r.get("body", ""),
            })

        return self._filter_blacklisted(results, "DuckDuckGo", brand)

    def _filter_blacklisted(self, results: list[dict], engine: str, brand: str) -> list[dict]:
        """Filter out blacklisted domains from search results."""
        filtered = []
        for r in results:
            domain = self._extract_domain(r["url"])
            if not any(bl in domain for bl in BLACKLISTED_DOMAINS):
                filtered.append(r)

        logger.info(
            "%s search for '%s': %d results (%d after filtering)",
            engine, brand, len(results), len(filtered),
        )
        return filtered

    # ------------------------------------------------------------------ #
    #  URL SCORING                                                        #
    # ------------------------------------------------------------------ #

    def _score_results(self, results: list[dict], brand: str) -> list[dict]:
        """
        Score and rank search results by relevance to wholesale contact info.
        Returns results sorted by score (descending).
        """
        brand_lower = brand.lower()
        scored = []

        for r in results:
            score = 0.0
            url_lower = r["url"].lower()
            title_lower = r.get("title", "").lower()
            snippet_lower = r.get("snippet", "").lower()
            domain = self._extract_domain(r["url"])

            # Brand name in domain = strong signal
            brand_slug = brand_lower.replace(" ", "")
            if brand_slug in domain.replace(".", "").replace("-", ""):
                score += 30

            # URL path keywords
            for kw in WHOLESALE_URL_KEYWORDS:
                if kw in url_lower:
                    score += 10

            # Title keywords
            for kw in WHOLESALE_CONTENT_KEYWORDS:
                if kw in title_lower:
                    score += 5

            # Snippet keywords
            for kw in WHOLESALE_CONTENT_KEYWORDS:
                if kw in snippet_lower:
                    score += 3

            # Penalize non-brand domains that look like directories/aggregators
            aggregator_signals = [
                "thomasnet.com", "globalindustrial.com", "grainger.com",
                "homedepot.com", "lowes.com", "mcmaster.com",
            ]
            if any(agg in domain for agg in aggregator_signals):
                score -= 10

            r["_score"] = score
            scored.append(r)

        scored.sort(key=lambda x: x["_score"], reverse=True)
        return scored

    # ------------------------------------------------------------------ #
    #  PAGE EXTRACTION                                                    #
    # ------------------------------------------------------------------ #

    async def _extract_from_page(self, url: str, brand: str) -> dict:
        """
        Visit a URL and extract contact info using regex patterns.
        Returns dict with emails, phones, urls, content snippets.
        """
        extraction = {
            "url": url,
            "emails": [],
            "phones": [],
            "wholesale_urls": [],
            "application_urls": [],
            "content_signals": [],
            "page_title": "",
        }

        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.warning("Failed to load %s: %s", url, e)
            return extraction

        # Get page title
        extraction["page_title"] = await self.page.title() or ""

        # Get visible text content
        text = await self.page.evaluate("""
            () => {
                // Get text content, excluding scripts/styles
                const clone = document.body.cloneNode(true);
                const remove = clone.querySelectorAll('script, style, noscript, svg');
                remove.forEach(el => el.remove());
                return clone.textContent.replace(/\\s+/g, ' ').substring(0, 50000);
            }
        """)

        # Extract emails
        raw_emails = EMAIL_PATTERN.findall(text)
        for email in raw_emails:
            email_lower = email.lower()
            domain = email_lower.split("@")[1] if "@" in email_lower else ""
            if domain not in JUNK_EMAIL_DOMAINS and not email_lower.endswith(".png"):
                extraction["emails"].append(email_lower)
        extraction["emails"] = list(dict.fromkeys(extraction["emails"]))  # dedup

        # Extract phones
        raw_phones = PHONE_PATTERN.findall(text)
        extraction["phones"] = list(dict.fromkeys(raw_phones))[:5]

        # Find wholesale/application links on the page
        links = await self.page.evaluate("""
            () => {
                const results = [];
                const anchors = document.querySelectorAll('a[href]');
                for (const a of anchors) {
                    const href = a.getAttribute('href');
                    const text = a.textContent.trim().toLowerCase();
                    if (href && (href.startsWith('http') || href.startsWith('/'))) {
                        results.push({href, text});
                    }
                }
                return results;
            }
        """)

        for link in links:
            href = link["href"].lower()
            text = link["text"]
            if any(kw in href or kw in text for kw in [
                "wholesale", "dealer", "trade", "b2b", "commercial", "pro-account",
            ]):
                full_url = link["href"] if link["href"].startswith("http") else url.rstrip("/") + link["href"]
                extraction["wholesale_urls"].append(full_url)
            if any(kw in href or kw in text for kw in [
                "apply", "application", "register", "sign-up", "signup", "open-account",
            ]):
                full_url = link["href"] if link["href"].startswith("http") else url.rstrip("/") + link["href"]
                extraction["application_urls"].append(full_url)

        extraction["wholesale_urls"] = list(dict.fromkeys(extraction["wholesale_urls"]))[:5]
        extraction["application_urls"] = list(dict.fromkeys(extraction["application_urls"]))[:5]

        # Check content for wholesale signals
        text_lower = text.lower()
        for kw in WHOLESALE_CONTENT_KEYWORDS:
            if kw in text_lower:
                # Extract surrounding context (80 chars around match)
                idx = text_lower.index(kw)
                start = max(0, idx - 40)
                end = min(len(text), idx + len(kw) + 40)
                snippet = text[start:end].strip()
                extraction["content_signals"].append(snippet)

        extraction["content_signals"] = extraction["content_signals"][:10]

        logger.info(
            "Extracted from %s: %d emails, %d phones, %d wholesale links, %d signals",
            url,
            len(extraction["emails"]),
            len(extraction["phones"]),
            len(extraction["wholesale_urls"]),
            len(extraction["content_signals"]),
        )
        return extraction

    # ------------------------------------------------------------------ #
    #  BRAND SUBPAGE CHECK                                                #
    # ------------------------------------------------------------------ #

    async def _check_brand_subpages(self, base_url: str, brand: str) -> dict | None:
        """
        If we're on a brand's main site, check common subpages for contact info.
        Returns extraction dict or None if no subpage found useful info.
        """
        domain = self._extract_domain(base_url)
        brand_slug = brand.lower().replace(" ", "")
        if brand_slug not in domain.replace(".", "").replace("-", ""):
            return None  # Not the brand's own site

        subpaths = [
            "/contact", "/contact-us", "/wholesale", "/dealers",
            "/become-a-dealer", "/trade", "/b2b", "/commercial",
            "/pro", "/professional",
        ]

        base = base_url.rstrip("/").split("?")[0].split("#")[0]
        # Strip path to get root
        from urllib.parse import urlparse
        parsed = urlparse(base)
        root = f"{parsed.scheme}://{parsed.netloc}"

        best = None
        best_score = 0

        for subpath in subpaths:
            sub_url = root + subpath
            try:
                resp = await self.page.goto(sub_url, wait_until="domcontentloaded", timeout=8000)
                if resp and resp.status == 200:
                    extraction = await self._extract_from_page(sub_url, brand)
                    score = (
                        len(extraction["emails"]) * 5
                        + len(extraction["wholesale_urls"]) * 3
                        + len(extraction["content_signals"]) * 2
                    )
                    if score > best_score:
                        best = extraction
                        best_score = score
            except Exception:
                continue

        return best

    # ------------------------------------------------------------------ #
    #  RESULT COMPILATION                                                 #
    # ------------------------------------------------------------------ #

    async def _compile_result(
        self, brand: str, extractions: list[dict]
    ) -> SupplierResult:
        """
        Merge extractions from multiple pages into a single SupplierResult.
        Classifies the supplier status based on signals found.
        """
        result = SupplierResult(brand=brand)

        all_emails = []
        all_phones = []
        all_wholesale_urls = []
        all_application_urls = []
        all_signals = []
        all_source_urls = []

        for ext in extractions:
            all_emails.extend(ext.get("emails", []))
            all_phones.extend(ext.get("phones", []))
            all_wholesale_urls.extend(ext.get("wholesale_urls", []))
            all_application_urls.extend(ext.get("application_urls", []))
            all_signals.extend(ext.get("content_signals", []))
            if ext.get("url"):
                all_source_urls.append(ext["url"])

        # Deduplicate
        result.emails = list(dict.fromkeys(all_emails))[:10]
        result.phones = list(dict.fromkeys(all_phones))[:5]
        result.source_urls = list(dict.fromkeys(all_source_urls))

        # Pick best wholesale/application URLs
        if all_wholesale_urls:
            result.wholesale_url = all_wholesale_urls[0]
        if all_application_urls:
            result.application_url = all_application_urls[0]

        # Set website to the brand's own domain if found
        brand_slug = brand.lower().replace(" ", "")
        for url in all_source_urls:
            domain = self._extract_domain(url)
            if brand_slug in domain.replace(".", "").replace("-", ""):
                from urllib.parse import urlparse
                parsed = urlparse(url)
                result.website = f"{parsed.scheme}://{parsed.netloc}"
                break

        # Extract requirements/restrictions from signals
        signals_text = " ".join(all_signals).lower()
        if "minimum order" in signals_text or "moq" in signals_text:
            for sig in all_signals:
                if "minimum" in sig.lower() or "moq" in sig.lower():
                    result.min_order = sig.strip()
                    break

        if "tax id" in signals_text or "resale certificate" in signals_text:
            result.requirements.append("Resale certificate / Tax ID required")
        if "business license" in signals_text:
            result.requirements.append("Business license required")
        if "brick and mortar" in signals_text or "brick-and-mortar" in signals_text:
            result.restrictions.append("Brick-and-mortar store required")
        if "no amazon" in signals_text or "not on amazon" in signals_text:
            result.restrictions.append("No Amazon sales allowed")
        if "map pricing" in signals_text or "map policy" in signals_text:
            result.restrictions.append("MAP pricing policy enforced")

        # Classify status
        result.status, result.confidence = self._classify_status(
            extractions, all_signals, all_wholesale_urls, all_application_urls,
        )

        # If ambiguous and Claude is enabled, try Claude fallback
        if (
            result.status == SupplierStatus.UNKNOWN
            and result.confidence < 0.4
            and self.use_claude
        ):
            claude_result = await self._claude_interpret(brand, extractions)
            if claude_result:
                result.status = claude_result.get("status", result.status)
                result.confidence = claude_result.get("confidence", result.confidence)
                result.notes = claude_result.get("notes", "")
                result.method = "claude_fallback"
                if claude_result.get("emails"):
                    for e in claude_result["emails"]:
                        if e not in result.emails:
                            result.emails.append(e)

        return result

    def _classify_status(
        self,
        extractions: list[dict],
        signals: list[str],
        wholesale_urls: list[str],
        application_urls: list[str],
    ) -> tuple[SupplierStatus, float]:
        """Classify supplier status based on extracted signals."""
        signals_text = " ".join(signals).lower()

        score_wholesale = 0
        score_application = 0
        score_distributor = 0
        score_retail = 0

        # Wholesale direct signals
        if wholesale_urls:
            score_wholesale += 20
        if any(kw in signals_text for kw in [
            "wholesale pricing", "wholesale catalog", "wholesale order",
            "dealer pricing", "volume discount",
        ]):
            score_wholesale += 15

        # Application required signals
        if application_urls:
            score_application += 20
        if any(kw in signals_text for kw in [
            "apply now", "dealer application", "open an account",
            "become a dealer", "apply to become",
        ]):
            score_application += 15
        if any(kw in signals_text for kw in [
            "resale certificate", "tax id", "business license",
        ]):
            score_application += 10

        # Distributor only signals
        if any(kw in signals_text for kw in [
            "distributor only", "through distributors", "authorized distributor",
            "find a distributor", "dealer locator",
        ]):
            score_distributor += 15

        # Retail only signals
        if any(kw in signals_text for kw in [
            "retail only", "consumer only", "buy now", "shop now",
            "add to cart",
        ]):
            score_retail += 5

        # Count emails as supporting evidence
        total_emails = sum(len(ext.get("emails", [])) for ext in extractions)
        if total_emails > 0:
            score_wholesale += 5
            score_application += 5

        scores = {
            SupplierStatus.WHOLESALE_DIRECT: score_wholesale,
            SupplierStatus.APPLICATION_REQUIRED: score_application,
            SupplierStatus.DISTRIBUTOR_ONLY: score_distributor,
            SupplierStatus.RETAIL_ONLY: score_retail,
        }

        best_status = max(scores, key=scores.get)
        best_score = scores[best_status]

        if best_score < 10:
            return SupplierStatus.UNKNOWN, 0.1

        # Confidence = normalized score (0-1)
        confidence = min(best_score / 50.0, 1.0)
        return best_status, round(confidence, 2)

    # ------------------------------------------------------------------ #
    #  CLAUDE CLI FALLBACK                                                #
    # ------------------------------------------------------------------ #

    async def _claude_interpret(
        self, brand: str, extractions: list[dict]
    ) -> dict | None:
        """
        Use Claude CLI to interpret ambiguous extraction results.
        Returns a dict with status, confidence, notes, emails or None on failure.
        """
        # Build context from extractions
        context_parts = []
        for ext in extractions:
            part = f"URL: {ext.get('url', 'N/A')}\n"
            part += f"Title: {ext.get('page_title', 'N/A')}\n"
            if ext.get("emails"):
                part += f"Emails: {', '.join(ext['emails'])}\n"
            if ext.get("phones"):
                part += f"Phones: {', '.join(ext['phones'])}\n"
            if ext.get("content_signals"):
                part += f"Signals: {'; '.join(ext['content_signals'][:5])}\n"
            context_parts.append(part)

        context = "\n---\n".join(context_parts)

        prompt = f"""Analyze this supplier research data for the brand "{brand}".

{context}

Respond with ONLY a JSON object (no markdown, no explanation):
{{
  "status": "WHOLESALE_DIRECT" | "APPLICATION_REQUIRED" | "DISTRIBUTOR_ONLY" | "RETAIL_ONLY" | "UNKNOWN",
  "confidence": 0.0-1.0,
  "notes": "brief explanation",
  "emails": ["any additional emails you spotted"]
}}"""

        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "-p", prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )

            if proc.returncode != 0:
                logger.warning("Claude CLI failed: %s", stderr.decode().strip())
                return None

            response = stdout.decode().strip()
            # Extract JSON from response
            json_match = re.search(r'\{[^{}]*\}', response, re.DOTALL)
            if not json_match:
                logger.warning("Claude response had no JSON: %s", response[:200])
                return None

            data = json.loads(json_match.group())

            # Validate status
            valid_statuses = {s.value for s in SupplierStatus}
            if data.get("status") in valid_statuses:
                data["status"] = SupplierStatus(data["status"])
            else:
                data["status"] = SupplierStatus.UNKNOWN

            logger.info(
                "Claude interpreted %s as %s (confidence: %s)",
                brand, data["status"], data.get("confidence"),
            )
            return data

        except asyncio.TimeoutError:
            logger.warning("Claude CLI timed out for brand '%s'", brand)
            return None
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to parse Claude response: %s", e)
            return None

    # ------------------------------------------------------------------ #
    #  PUBLIC API                                                         #
    # ------------------------------------------------------------------ #

    async def research(
        self, brand: str, category: str = ""
    ) -> SupplierResult:
        """
        Research a single brand. Returns a SupplierResult with contact info
        and classification.

        Args:
            brand: Brand name to research (e.g. "Kinco").
            category: Optional product category for better search results.
        """
        logger.info("Researching brand: %s (category: %s)", brand, category or "none")

        # Step 1: Search (Google → DuckDuckGo fallback)
        results = await self._search(brand, category)
        if not results:
            logger.warning("No Google results for '%s'", brand)
            return SupplierResult(brand=brand, notes="No search results found")

        # Step 2: Score and rank
        scored = self._score_results(results, brand)

        # Step 3: Visit top pages and extract
        extractions = []
        for r in scored[:MAX_PAGES_PER_BRAND]:
            ext = await self._extract_from_page(r["url"], brand)
            extractions.append(ext)

        # Step 4: Check brand subpages if we found the brand's own site
        for r in scored[:MAX_PAGES_PER_BRAND]:
            subpage_ext = await self._check_brand_subpages(r["url"], brand)
            if subpage_ext:
                extractions.append(subpage_ext)
                break  # Only need one brand site deep-dive

        # Step 5: Compile and classify
        result = await self._compile_result(brand, extractions)

        logger.info(
            "Brand '%s' → status=%s, confidence=%.2f, emails=%d, phones=%d",
            brand, result.status.value, result.confidence,
            len(result.emails), len(result.phones),
        )
        return result

    async def research_batch(
        self,
        brands: list[dict],
        delay: float = BATCH_DELAY,
        progress_callback=None,
    ) -> list[SupplierResult]:
        """
        Research multiple brands with rate limiting.

        Args:
            brands: List of dicts with 'brand' and optional 'category' keys.
            delay: Seconds between brands (default 8s for Google rate limiting).
            progress_callback: Optional callable(index, total, result) for progress.

        Returns:
            List of SupplierResult objects.
        """
        results = []
        total = len(brands)

        for i, entry in enumerate(brands):
            brand = entry["brand"] if isinstance(entry, dict) else str(entry)
            category = entry.get("category", "") if isinstance(entry, dict) else ""

            logger.info("--- Brand %d/%d: %s ---", i + 1, total, brand)

            try:
                result = await self.research(brand, category)
            except Exception as e:
                logger.error("Failed to research '%s': %s", brand, e)
                result = SupplierResult(
                    brand=brand,
                    notes=f"Research failed: {e}",
                )
                if self.screenshot_dir:
                    try:
                        await self.page.screenshot(
                            path=os.path.join(
                                self.screenshot_dir, f"error_{brand}.png"
                            )
                        )
                    except Exception:
                        pass

            results.append(result)

            if progress_callback:
                try:
                    progress_callback(i + 1, total, result)
                except Exception:
                    pass

            # Rate limit between brands (skip after last)
            if i < total - 1:
                logger.info("Waiting %.1fs before next brand...", delay)
                await asyncio.sleep(delay)

        return results

    # ------------------------------------------------------------------ #
    #  UTILITIES                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_domain(url: str) -> str:
        """Extract domain from URL."""
        try:
            from urllib.parse import urlparse
            return urlparse(url).netloc.lower().lstrip("www.")
        except Exception:
            return ""

    @staticmethod
    def results_to_csv(results: list[SupplierResult], path: str):
        """Export results to CSV."""
        import csv
        if not results:
            return

        fieldnames = [
            "brand", "status", "confidence", "website", "wholesale_url",
            "application_url", "emails", "phones", "requirements",
            "restrictions", "min_order", "notes", "method", "source_urls",
        ]

        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in results:
                row = r.to_dict()
                # Flatten lists to semicolon-separated strings
                for key in ["emails", "phones", "requirements", "restrictions", "source_urls"]:
                    if isinstance(row.get(key), list):
                        row[key] = "; ".join(row[key])
                writer.writerow(row)

        logger.info("Exported %d results to %s", len(results), path)


# ------------------------------------------------------------------ #
#  STANDALONE CLI                                                     #
# ------------------------------------------------------------------ #

async def _cli_main():
    """Standalone CLI entry point for quick brand lookups."""
    import argparse
    from playwright.async_api import async_playwright
    from playwright_stealth import Stealth

    parser = argparse.ArgumentParser(
        description="Research supplier/wholesale contact info for a brand"
    )
    parser.add_argument("--brand", required=True, help="Brand name to research")
    parser.add_argument("--category", default="", help="Product category (optional)")
    parser.add_argument("--claude", action="store_true", help="Enable Claude CLI fallback")
    parser.add_argument("--output", default="", help="Output CSV path (optional)")
    parser.add_argument("--headless", action="store_true", help="Run browser headless")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=args.headless,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context()
    page = await context.new_page()

    stealth = Stealth()
    await stealth.apply_stealth_async(page)

    researcher = SupplierResearcher(
        page,
        use_claude=args.claude,
        screenshot_dir="supplier_screenshots",
    )

    result = await researcher.research(args.brand, args.category)

    print("\n" + "=" * 60)
    print(f"  Brand: {result.brand}")
    print(f"  Status: {result.status.value}")
    print(f"  Confidence: {result.confidence:.0%}")
    if result.website:
        print(f"  Website: {result.website}")
    if result.emails:
        print(f"  Emails: {', '.join(result.emails)}")
    if result.phones:
        print(f"  Phones: {', '.join(result.phones)}")
    if result.wholesale_url:
        print(f"  Wholesale URL: {result.wholesale_url}")
    if result.application_url:
        print(f"  Application URL: {result.application_url}")
    if result.requirements:
        print(f"  Requirements: {'; '.join(result.requirements)}")
    if result.restrictions:
        print(f"  Restrictions: {'; '.join(result.restrictions)}")
    if result.min_order:
        print(f"  Min Order: {result.min_order}")
    if result.notes:
        print(f"  Notes: {result.notes}")
    print(f"  Method: {result.method}")
    print(f"  Sources: {', '.join(result.source_urls)}")
    print("=" * 60 + "\n")

    if args.output:
        SupplierResearcher.results_to_csv([result], args.output)

    print(json.dumps(result.to_dict(), indent=2))

    await browser.close()
    await pw.stop()


if __name__ == "__main__":
    asyncio.run(_cli_main())
