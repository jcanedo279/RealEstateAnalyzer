"""
Backward-compatible entrypoint.

The project README and older automation scripts invoke:

  python -m re_analyzer.scrapers.conservative_scraper_runner

The implementation was consolidated into `scraper_runner.py`, but the module
name is intentionally kept as a thin wrapper so existing commands continue to
work.
"""

from re_analyzer.scrapers.scraper_runner import main


if __name__ == "__main__":
    main()

