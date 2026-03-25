"""
Quick setup for integrating keepa_automation into FBA Command Center.

Usage from FBA Command Center repo:
    pip install -e /path/to/Playwright-Engine-

Then in your code:
    from keepa_automation import KeepaAutomation
"""

from setuptools import setup

setup(
    name="keepa-automation",
    version="1.0.0",
    description="Keepa Product Finder Playwright automation — full result extraction",
    py_modules=["keepa_automation"],
    install_requires=[
        "playwright>=1.40.0",
        "playwright-stealth>=2.0.0",
        "pandas>=2.0.0",
    ],
    python_requires=">=3.10",
)
