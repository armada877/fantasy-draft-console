"""Scrapers and external-source adapters.

This package marker exists so `python3 -m scraping.sources` works from the repo
root. The legacy scripts in here (scrape.py, scrape_league.py, ...) are still run
as standalone files with `scraping/` on sys.path, which is unaffected.
"""
