from re_analyzer.scrapers.scraping_utility import _parse_chrome_major_version


def test_parse_chrome_major_version_parses_google_chrome_output():
    assert _parse_chrome_major_version("Google Chrome 148.0.7778.178") == 148


def test_parse_chrome_major_version_parses_chromium_output():
    assert _parse_chrome_major_version("Chromium 120.0.6099.0") == 120


def test_parse_chrome_major_version_returns_none_on_empty():
    assert _parse_chrome_major_version("") is None

