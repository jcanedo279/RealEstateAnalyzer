from pathlib import Path

from re_analyzer.scrapers.browser_diagnostics import (
    redact_path,
    redact_paths_in_args,
    summarize_capabilities,
)


def test_redact_path_replaces_home_prefix():
    home = str(Path.home())
    if not home:
        return
    raw = f"{home}/Library/Application Support/Google/Chrome"
    redacted = redact_path(raw)
    assert redacted.startswith("~")
    assert home not in redacted


def test_redact_paths_in_args_redacts_equals_form():
    home = str(Path.home())
    raw_args = [
        f"--user-data-dir={home}/ChromeUserData",
        "--window-size=1280,900",
    ]
    redacted = redact_paths_in_args(raw_args)
    assert any(arg.startswith("--user-data-dir=~") for arg in redacted)
    assert "--window-size=1280,900" in redacted


def test_summarize_capabilities_extracts_versions_and_redacts_paths():
    capabilities = {
        "browserName": "chrome",
        "browserVersion": "126.0.6478.115",
        "platformName": "mac",
        "acceptInsecureCerts": True,
        "timeouts": {"implicit": 0, "pageLoad": 300000, "script": 30000},
        "chrome": {
            "chromedriverVersion": "126.0.6478.126 (d36ace6122e...)",
            "userDataDir": f"{Path.home()}/Library/Application Support/Google/Chrome/Profile 1",
        },
        "se:cdp": "ws://localhost:1234/devtools/browser/abc",
        "se:cdpVersion": "126.0.6478.115",
    }

    summary = summarize_capabilities(capabilities)
    assert summary["browser_name"] == "chrome"
    assert summary["browser_version"] == "126.0.6478.115"
    assert summary["chromedriver"]["version"] == "126.0.6478.126"
    assert summary["chromedriver"]["user_data_dir"].startswith("~")

