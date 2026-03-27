"""
Tests for supplier_research module.

Covers URL scoring, regex extraction, status classification,
blacklist filtering, and result compilation — all without needing
a live browser or Google access.

Run:  python -m pytest test_supplier_research.py -v
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supplier_research import (
    BLACKLISTED_DOMAINS,
    EMAIL_PATTERN,
    PHONE_PATTERN,
    SupplierResearcher,
    SupplierResult,
    SupplierStatus,
)


# ------------------------------------------------------------------ #
#  FIXTURES                                                           #
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_page():
    """Create a mock Playwright Page object."""
    page = AsyncMock()
    page.goto = AsyncMock()
    page.content = AsyncMock(return_value="<html></html>")
    page.title = AsyncMock(return_value="Test Page")
    page.evaluate = AsyncMock(return_value=[])
    page.screenshot = AsyncMock()
    return page


@pytest.fixture
def researcher(mock_page):
    """Create a SupplierResearcher with a mock page."""
    return SupplierResearcher(mock_page, use_claude=False)


# ------------------------------------------------------------------ #
#  EMAIL REGEX                                                        #
# ------------------------------------------------------------------ #

class TestEmailPattern:
    def test_standard_email(self):
        assert EMAIL_PATTERN.findall("contact@example.com") == ["contact@example.com"]

    def test_email_with_dots(self):
        assert EMAIL_PATTERN.findall("john.doe@company.co.uk") == ["john.doe@company.co.uk"]

    def test_email_with_plus(self):
        assert EMAIL_PATTERN.findall("user+tag@example.com") == ["user+tag@example.com"]

    def test_no_email(self):
        assert EMAIL_PATTERN.findall("no email here") == []

    def test_multiple_emails(self):
        text = "Contact sales@brand.com or wholesale@brand.com for info"
        result = EMAIL_PATTERN.findall(text)
        assert "sales@brand.com" in result
        assert "wholesale@brand.com" in result

    def test_email_in_url_context(self):
        text = "Email: info@kinco.com Phone: 555-1234"
        assert "info@kinco.com" in EMAIL_PATTERN.findall(text)


# ------------------------------------------------------------------ #
#  PHONE REGEX                                                        #
# ------------------------------------------------------------------ #

class TestPhonePattern:
    def test_standard_phone(self):
        assert PHONE_PATTERN.findall("Call 555-123-4567") == ["555-123-4567"]

    def test_parenthesized_area_code(self):
        assert PHONE_PATTERN.findall("(800) 555-1234") == ["(800) 555-1234"]

    def test_dotted_format(self):
        assert PHONE_PATTERN.findall("800.555.1234") == ["800.555.1234"]

    def test_with_country_code(self):
        matches = PHONE_PATTERN.findall("+1-800-555-1234")
        assert len(matches) >= 1

    def test_no_phone(self):
        assert PHONE_PATTERN.findall("no phone here") == []


# ------------------------------------------------------------------ #
#  URL SCORING                                                        #
# ------------------------------------------------------------------ #

class TestScoreResults:
    def test_brand_domain_scores_highest(self, researcher):
        results = [
            {"url": "https://www.kinco.com/wholesale", "title": "Wholesale", "snippet": ""},
            {"url": "https://www.randomsite.com/kinco", "title": "Kinco Products", "snippet": ""},
        ]
        scored = researcher._score_results(results, "Kinco")
        assert scored[0]["url"] == "https://www.kinco.com/wholesale"

    def test_wholesale_keyword_boosts_score(self, researcher):
        results = [
            {"url": "https://brand.com/wholesale", "title": "Wholesale", "snippet": ""},
            {"url": "https://brand.com/about", "title": "About Us", "snippet": ""},
        ]
        scored = researcher._score_results(results, "SomeBrand")
        assert scored[0]["url"] == "https://brand.com/wholesale"

    def test_aggregator_penalized(self, researcher):
        results = [
            {"url": "https://www.grainger.com/kinco", "title": "Kinco at Grainger", "snippet": ""},
            {"url": "https://www.kinco.com/", "title": "Kinco Home", "snippet": ""},
        ]
        scored = researcher._score_results(results, "Kinco")
        # Brand domain should beat aggregator
        assert scored[0]["url"] == "https://www.kinco.com/"

    def test_empty_results(self, researcher):
        assert researcher._score_results([], "Brand") == []

    def test_snippet_keywords_add_score(self, researcher):
        results = [
            {"url": "https://a.com", "title": "Page", "snippet": "wholesale pricing and dealer program"},
            {"url": "https://b.com", "title": "Page", "snippet": "random unrelated content"},
        ]
        scored = researcher._score_results(results, "Brand")
        assert scored[0]["url"] == "https://a.com"


# ------------------------------------------------------------------ #
#  BLACKLIST FILTERING                                                #
# ------------------------------------------------------------------ #

class TestBlacklist:
    def test_amazon_blocked(self):
        assert "amazon.com" in BLACKLISTED_DOMAINS

    def test_ebay_blocked(self):
        assert "ebay.com" in BLACKLISTED_DOMAINS

    def test_social_media_blocked(self):
        assert "facebook.com" in BLACKLISTED_DOMAINS
        assert "instagram.com" in BLACKLISTED_DOMAINS
        assert "youtube.com" in BLACKLISTED_DOMAINS


# ------------------------------------------------------------------ #
#  DOMAIN EXTRACTION                                                  #
# ------------------------------------------------------------------ #

class TestExtractDomain:
    def test_standard_url(self):
        assert SupplierResearcher._extract_domain("https://www.kinco.com/page") == "kinco.com"

    def test_url_with_path(self):
        domain = SupplierResearcher._extract_domain("https://brand.com/wholesale/apply")
        assert domain == "brand.com"

    def test_subdomain(self):
        domain = SupplierResearcher._extract_domain("https://shop.brand.com/page")
        assert "brand.com" in domain

    def test_empty_url(self):
        assert SupplierResearcher._extract_domain("") == ""

    def test_invalid_url(self):
        assert SupplierResearcher._extract_domain("not-a-url") == ""


# ------------------------------------------------------------------ #
#  STATUS CLASSIFICATION                                              #
# ------------------------------------------------------------------ #

class TestClassifyStatus:
    def test_wholesale_signals(self, researcher):
        extractions = [{"emails": ["a@b.com"]}]
        signals = ["wholesale pricing available", "dealer pricing for bulk orders"]
        status, confidence = researcher._classify_status(
            extractions, signals,
            wholesale_urls=["https://brand.com/wholesale"],
            application_urls=[],
        )
        assert status == SupplierStatus.WHOLESALE_DIRECT
        assert confidence > 0.3

    def test_application_required(self, researcher):
        extractions = [{"emails": []}]
        signals = ["apply now to become a dealer", "dealer application form", "resale certificate required"]
        status, confidence = researcher._classify_status(
            extractions, signals,
            wholesale_urls=[],
            application_urls=["https://brand.com/apply"],
        )
        assert status == SupplierStatus.APPLICATION_REQUIRED

    def test_distributor_only(self, researcher):
        extractions = [{"emails": []}]
        signals = ["products available through distributors only", "find a distributor near you"]
        status, confidence = researcher._classify_status(
            extractions, signals,
            wholesale_urls=[],
            application_urls=[],
        )
        assert status == SupplierStatus.DISTRIBUTOR_ONLY

    def test_unknown_no_signals(self, researcher):
        extractions = [{"emails": []}]
        status, confidence = researcher._classify_status(
            extractions, signals=[],
            wholesale_urls=[],
            application_urls=[],
        )
        assert status == SupplierStatus.UNKNOWN
        assert confidence < 0.2


# ------------------------------------------------------------------ #
#  SUPPLIER RESULT                                                    #
# ------------------------------------------------------------------ #

class TestSupplierResult:
    def test_default_values(self):
        r = SupplierResult(brand="TestBrand")
        assert r.brand == "TestBrand"
        assert r.status == SupplierStatus.UNKNOWN
        assert r.emails == []
        assert r.confidence == 0.0

    def test_to_dict(self):
        r = SupplierResult(
            brand="Kinco",
            status=SupplierStatus.WHOLESALE_DIRECT,
            emails=["info@kinco.com"],
            confidence=0.8,
        )
        d = r.to_dict()
        assert d["brand"] == "Kinco"
        assert d["status"] == "WHOLESALE_DIRECT"
        assert d["emails"] == ["info@kinco.com"]
        assert d["confidence"] == 0.8

    def test_to_dict_serializable(self):
        """Ensure to_dict output is JSON-serializable."""
        import json
        r = SupplierResult(
            brand="Test",
            status=SupplierStatus.APPLICATION_REQUIRED,
            emails=["a@b.com"],
            phones=["555-1234"],
            requirements=["Tax ID"],
        )
        # Should not raise
        json_str = json.dumps(r.to_dict())
        assert "APPLICATION_REQUIRED" in json_str


# ------------------------------------------------------------------ #
#  COMPILE RESULT (async)                                             #
# ------------------------------------------------------------------ #

class TestCompileResult:
    @pytest.mark.asyncio
    async def test_merges_extractions(self, researcher):
        extractions = [
            {
                "url": "https://kinco.com/wholesale",
                "emails": ["sales@kinco.com"],
                "phones": ["800-555-1234"],
                "wholesale_urls": ["https://kinco.com/wholesale"],
                "application_urls": [],
                "content_signals": ["wholesale pricing available"],
                "page_title": "Kinco Wholesale",
            },
            {
                "url": "https://kinco.com/contact",
                "emails": ["info@kinco.com"],
                "phones": [],
                "wholesale_urls": [],
                "application_urls": [],
                "content_signals": ["dealer program information"],
                "page_title": "Contact Kinco",
            },
        ]
        result = await researcher._compile_result("Kinco", extractions)
        assert result.brand == "Kinco"
        assert "sales@kinco.com" in result.emails
        assert "info@kinco.com" in result.emails
        assert "800-555-1234" in result.phones
        assert result.wholesale_url == "https://kinco.com/wholesale"

    @pytest.mark.asyncio
    async def test_deduplicates_emails(self, researcher):
        extractions = [
            {
                "url": "https://a.com",
                "emails": ["dup@brand.com", "dup@brand.com"],
                "phones": [],
                "wholesale_urls": [],
                "application_urls": [],
                "content_signals": [],
                "page_title": "",
            },
        ]
        result = await researcher._compile_result("Brand", extractions)
        assert result.emails.count("dup@brand.com") == 1

    @pytest.mark.asyncio
    async def test_detects_requirements(self, researcher):
        extractions = [
            {
                "url": "https://brand.com",
                "emails": [],
                "phones": [],
                "wholesale_urls": [],
                "application_urls": [],
                "content_signals": ["Must provide resale certificate and tax id"],
                "page_title": "",
            },
        ]
        result = await researcher._compile_result("Brand", extractions)
        assert any("resale" in r.lower() or "tax" in r.lower() for r in result.requirements)

    @pytest.mark.asyncio
    async def test_empty_extractions(self, researcher):
        result = await researcher._compile_result("Empty", [])
        assert result.brand == "Empty"
        assert result.status == SupplierStatus.UNKNOWN


# ------------------------------------------------------------------ #
#  CSV EXPORT                                                         #
# ------------------------------------------------------------------ #

class TestResultsToCSV:
    def test_export_creates_file(self, tmp_path):
        results = [
            SupplierResult(
                brand="Kinco",
                status=SupplierStatus.WHOLESALE_DIRECT,
                emails=["info@kinco.com"],
                confidence=0.8,
            ),
        ]
        csv_path = str(tmp_path / "test_output.csv")
        SupplierResearcher.results_to_csv(results, csv_path)
        assert os.path.exists(csv_path)

        import csv
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["brand"] == "Kinco"
        assert rows[0]["status"] == "WHOLESALE_DIRECT"

    def test_export_empty_list(self, tmp_path):
        csv_path = str(tmp_path / "empty.csv")
        SupplierResearcher.results_to_csv([], csv_path)
        assert not os.path.exists(csv_path)


# ------------------------------------------------------------------ #
#  GOOGLE SEARCH (mocked)                                             #
# ------------------------------------------------------------------ #

class TestGoogleSearch:
    @pytest.mark.asyncio
    async def test_captcha_detection(self, researcher, mock_page):
        mock_page.content = AsyncMock(return_value="<html>unusual traffic from your computer</html>")
        mock_page.evaluate = AsyncMock(return_value=[])
        results = await researcher._google_search("TestBrand")
        assert results == []

    @pytest.mark.asyncio
    async def test_filters_blacklisted_domains(self, researcher, mock_page):
        mock_page.content = AsyncMock(return_value="<html></html>")
        mock_page.evaluate = AsyncMock(return_value=[
            {"url": "https://amazon.com/kinco", "title": "Kinco on Amazon", "snippet": ""},
            {"url": "https://kinco.com/wholesale", "title": "Kinco Wholesale", "snippet": ""},
        ])
        results = await researcher._google_search("Kinco")
        urls = [r["url"] for r in results]
        assert "https://amazon.com/kinco" not in urls
        assert "https://kinco.com/wholesale" in urls

    @pytest.mark.asyncio
    async def test_navigation_failure(self, researcher, mock_page):
        mock_page.goto = AsyncMock(side_effect=Exception("Network error"))
        results = await researcher._google_search("FailBrand")
        assert results == []


# ------------------------------------------------------------------ #
#  DUCKDUCKGO SEARCH (mocked)                                        #
# ------------------------------------------------------------------ #

class TestDuckDuckGoSearch:
    @pytest.mark.asyncio
    async def test_returns_results(self, researcher):
        fake_raw = [
            {"href": "https://kinco.com/wholesale", "title": "Kinco Wholesale", "body": "dealer info"},
            {"href": "https://kinco.com/contact", "title": "Contact Kinco", "body": ""},
        ]
        with patch("supplier_research.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.return_value = fake_raw
            results = await researcher._duckduckgo_search("Kinco")
        assert len(results) == 2
        assert results[0]["url"] == "https://kinco.com/wholesale"

    @pytest.mark.asyncio
    async def test_filters_blacklisted(self, researcher):
        fake_raw = [
            {"href": "https://ebay.com/kinco", "title": "Kinco on eBay", "body": ""},
            {"href": "https://kinco.com/", "title": "Kinco", "body": ""},
        ]
        with patch("supplier_research.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.return_value = fake_raw
            results = await researcher._duckduckgo_search("Kinco")
        urls = [r["url"] for r in results]
        assert "https://ebay.com/kinco" not in urls
        assert "https://kinco.com/" in urls

    @pytest.mark.asyncio
    async def test_api_failure(self, researcher):
        with patch("supplier_research.DDGS") as mock_ddgs:
            mock_ddgs.return_value.text.side_effect = Exception("API error")
            results = await researcher._duckduckgo_search("FailBrand")
        assert results == []


# ------------------------------------------------------------------ #
#  SEARCH FALLBACK (Google → DuckDuckGo)                             #
# ------------------------------------------------------------------ #

class TestSearchFallback:
    @pytest.mark.asyncio
    async def test_uses_google_when_available(self, researcher):
        google_results = [{"url": "https://kinco.com", "title": "Kinco", "snippet": ""}]
        researcher._google_search = AsyncMock(return_value=google_results)
        researcher._duckduckgo_search = AsyncMock(return_value=[])

        results = await researcher._search("Kinco")
        assert results == google_results
        researcher._duckduckgo_search.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_ddg_on_captcha(self, researcher):
        ddg_results = [{"url": "https://kinco.com", "title": "Kinco", "snippet": ""}]
        researcher._google_search = AsyncMock(return_value=[])  # CAPTCHA returns empty
        researcher._duckduckgo_search = AsyncMock(return_value=ddg_results)

        results = await researcher._search("Kinco")
        assert results == ddg_results
        researcher._duckduckgo_search.assert_called_once_with("Kinco", "")


# Need os for TestResultsToCSV
import os
