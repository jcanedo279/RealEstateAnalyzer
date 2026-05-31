import pathlib

import pytest

from re_analyzer.scrapers.page_diagnostics import detect_challenge
from re_analyzer.scrapers.provider_adapters import ProviderBlockedError, RealtorListingProvider


class StubDriver:
    def __init__(
        self,
        *,
        title: str = "",
        current_url: str = "",
        page_source: str = "",
        body_text: str = "",
        dom_payload: dict | None = None,
    ):
        self.title = title
        self.current_url = current_url
        self.page_source = page_source
        self._body_text = body_text
        self._dom_payload = dom_payload or {}

    def execute_script(self, script, *args):
        script = str(script or "")
        if script.strip() == "return document.body ? document.body.innerText : '';":
            return self._body_text
        if "px_captcha: false" in script and "recaptcha: false" in script:
            return self._dom_payload
        return None


def _load_fixture_text(rel_path: str) -> str:
    path = pathlib.Path(__file__).resolve().parent / "fixtures" / rel_path
    return path.read_text(encoding="utf-8")


def test_detect_challenge_flags_zillow_press_and_hold_fixture():
    html = _load_fixture_text("zillow_press_and_hold.html")
    body_text = "Before we continue... Press & Hold to confirm you are a human (and not a bot)."
    driver = StubDriver(
        title="Before we continue…",
        current_url="https://www.zillow.com/homes/33129_rb/",
        page_source=html,
        body_text=body_text,
        dom_payload={"px": False, "recaptcha": False},
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is True
    assert "px_captcha" in (challenge["matched_patterns"] or [])
    assert "press_and_hold" in (challenge["matched_patterns"] or [])


def test_detect_challenge_marks_soft_when_listing_content_present():
    # A listing page that embeds a reCAPTCHA widget for a contact/login form.
    # The DOM inspection finds the iframe but the page clearly has usable listing content,
    # so this should be a soft challenge (not a hard block).
    page_source = (
        "<html><body>"
        "<iframe src='https://www.google.com/recaptcha/api2/anchor'></iframe>"
        "<div>45 homes for sale</div>"
        "</body></html>"
    )
    body_text = (
        "45 homes for sale in Gainesville. Browse 3 bed, 2 bath homes from $200,000 to $500,000. "
        "Updated listings daily with photos and full descriptions of each property."
    )
    driver = StubDriver(
        title="Homes for sale",
        current_url="https://www.example.com/listings",
        page_source=page_source,
        body_text=body_text,
        dom_payload={
            "px_captcha": False,
            "px_instrumentation": False,
            "recaptcha": True,
            "hcaptcha": False,
            "cloudflare": False,
            "realtor_block": False,
        },
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is False
    assert challenge["is_soft_challenge"] is True
    assert challenge["has_usable_listing_content"] is True
    assert "recaptcha_frame" in (challenge["matched_patterns"] or [])


def test_detect_challenge_non_challenge_listing_page():
    page_source = "<html><body><h1>Homes for sale</h1></body></html>"
    body_text = "Homes for sale 45 homes $400,000 4 beds 3 baths"
    driver = StubDriver(
        title="Homes for sale",
        current_url="https://www.example.com/listings",
        page_source=page_source,
        body_text=body_text,
        dom_payload={"px": False, "recaptcha": False},
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is False
    assert challenge["is_soft_challenge"] is False
    assert challenge["has_usable_listing_content"] is True
    assert not (challenge["matched_patterns"] or [])


def test_detect_challenge_flags_perimeterx_access_denied_variant():
    page_source = (
        "<html><body>"
        "<div id='px-captcha'></div>"
        "<h1>Please verify you are a human</h1>"
        "<p>Access to this page has been denied because we believe you are using automation tools to browse the website.</p>"
        "<p>Powered by PerimeterX, Inc.</p>"
        "</body></html>"
    )
    body_text = (
        "Please verify you are a human\n"
        "Access to this page has been denied because we believe you are using automation tools to browse the website.\n"
        "Powered by PerimeterX, Inc."
    )
    driver = StubDriver(
        title="Please verify you are a human",
        current_url="https://www.example.com/protected",
        page_source=page_source,
        body_text=body_text,
        dom_payload={"px": False, "recaptcha": False},
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is True
    assert "px_captcha" in (challenge["matched_patterns"] or [])
    assert "access_denied" in (challenge["matched_patterns"] or [])


def test_detect_challenge_flags_akamai_access_denied_variant():
    page_source = "<html><body><h1>Access Denied</h1></body></html>"
    body_text = (
        "Access Denied\n"
        "You don't have permission to access \"http://www.microsoft.com\" on this server.\n"
        "Reference #18.85c5d617.1549635923.277a7a1"
    )
    driver = StubDriver(
        title="Access Denied",
        current_url="https://www.example.com/blocked",
        page_source=page_source,
        body_text=body_text,
        dom_payload={"px": False, "recaptcha": False},
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is True
    assert "access_denied" in (challenge["matched_patterns"] or [])


def test_detect_challenge_flags_recaptcha_frame_from_source():
    page_source = (
        "<html><body>"
        "<iframe src=\"https://www.google.com/recaptcha/api2/anchor?ar=1&k=sitekey\"></iframe>"
        "</body></html>"
    )
    driver = StubDriver(
        title="",
        current_url="https://www.example.com/recaptcha",
        page_source=page_source,
        body_text="",
        dom_payload={"px": False, "recaptcha": False},
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is True
    assert "recaptcha_frame" in (challenge["matched_patterns"] or [])


def test_detect_challenge_flags_hcaptcha_frame_from_source():
    page_source = (
        "<html><body>"
        "<iframe src=\"https://www.hcaptcha.com/1/api.js\"></iframe>"
        "</body></html>"
    )
    driver = StubDriver(
        title="",
        current_url="https://www.example.com/hcaptcha",
        page_source=page_source,
        body_text="",
        dom_payload={"px": False, "recaptcha": False},
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is True
    assert "hcaptcha_frame" in (challenge["matched_patterns"] or [])


def test_detect_challenge_flags_cloudflare_challenge():
    page_source = (
        "<html><body>"
        "<h1>Checking your browser before accessing</h1>"
        "<p>DDoS protection by Cloudflare</p>"
        "</body></html>"
    )
    driver = StubDriver(
        title="Checking your browser before accessing", 
        current_url="https://www.example.com/cloudflare",
        page_source=page_source,
        body_text="Checking your browser before accessing this site",
        dom_payload={"px": False, "recaptcha": False},
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is True
    assert "cloudflare_challenge" in (challenge["matched_patterns"] or [])


def test_detect_challenge_flags_realtor_kpsdk_block_page():
    body_text = (
        "Your request could not be processed.\n"
        "Please note that your reference ID is bbdf203f-7b5d-4a32-81a8-ff04610da56e.\n"
        "If this issue persists, please contact unblockrequest@realtor.com.\n"
        "window.KPSDK={};"
    )
    driver = StubDriver(
        title="",
        current_url="https://www.realtor.com/miscellaneous/userblocked/",
        page_source="<html><body><script>window.KPSDK={};</script></body></html>",
        body_text=body_text,
        dom_payload={"px": False, "recaptcha": False},
    )

    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is True
    assert "request_blocked" in (challenge["matched_patterns"] or [])


def test_realtor_blocked_page_detection_raises_provider_blocked_error():
    provider = RealtorListingProvider()
    driver = StubDriver(
        title="",
        current_url="https://www.realtor.com/miscellaneous/userblocked/",
        page_source="",
        body_text=(
            "Your request could not be processed.\n"
            "Please note that your reference ID is bbdf203f-7b5d-4a32-81a8-ff04610da56e.\n"
            "Contact unblockrequest@realtor.com\n"
            "window.KPSDK={};"
        ),
    )

    with pytest.raises(ProviderBlockedError) as excinfo:
        provider._raise_if_blocked_page(driver, context="unit_test")

    exc = excinfo.value
    assert exc.provider == "realtor"
    assert exc.reason in {"realtor_request_not_processed", "realtor_kpsdk_block", "realtor_blocked"}
    assert exc.reference_id == "bbdf203f-7b5d-4a32-81a8-ff04610da56e"
    assert "blocked" in str(exc).lower()


def test_realtor_blocked_body_heuristic():
    provider = RealtorListingProvider()
    assert provider._looks_like_blocked_response("{\"ok\":true}") is False
    assert provider._looks_like_blocked_response("<html><body>Your request could not be processed</body></html>") is True


_REALTOR_CSS_BLOCK_HTML = """\
<html><head></head><body>
    <style>
        .hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden;user-select:none;}
        .msg{color:transparent;animation:reveal 1s forwards 3s;-webkit-animation:reveal 1s forwards 3s;}
        @keyframes reveal{to{color:black;}}
    </style>
    <p class="msg">
        Your request could not be processed.<br>
        Please note that your reference ID is f9c13bfd-5f44-4575-a749-3bf28d820227.<br>
        If this issue persists, please contact
        <span style="position:relative;display:inline;">
            <span class="hp" aria-hidden="true">support@realtor-help.invalid</span>
            <span>unblockrequest@realtor.com</span>
            <span class="hp" aria-hidden="true">noreply@realtor.com</span>
        </span>
        with the above reference ID and any other pertinent details.
    </p>
</body></html>"""


def test_detect_challenge_flags_realtor_css_obfuscated_block():
    """color:transparent block page must be caught via page source even when innerText is empty."""
    driver = StubDriver(
        title="",
        current_url="https://www.realtor.com/realestateandhomes-search/32004",
        page_source=_REALTOR_CSS_BLOCK_HTML,
        body_text="",  # worst case: innerText returns nothing for transparent text
        dom_payload={"px": False, "recaptcha": False},
    )
    challenge = detect_challenge(driver)
    assert challenge["is_challenge"] is True
    assert "realtor_block" in (challenge["matched_patterns"] or [])


def test_realtor_css_obfuscated_block_raises_provider_blocked_error():
    """_raise_if_blocked_page must fall back to page source for the CSS-animated block variant."""
    provider = RealtorListingProvider()
    driver = StubDriver(
        title="",
        current_url="https://www.realtor.com/realestateandhomes-search/32004",
        page_source=_REALTOR_CSS_BLOCK_HTML,
        body_text="",  # simulate innerText missing transparent text
    )
    with pytest.raises(ProviderBlockedError) as excinfo:
        provider._raise_if_blocked_page(driver, context="unit_test")
    exc = excinfo.value
    assert exc.provider == "realtor"
    assert exc.reference_id == "f9c13bfd-5f44-4575-a749-3bf28d820227"
