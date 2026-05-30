from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List


VECTOR_BROWSER_ENV = "browser_env"
VECTOR_TLS_HTTP2 = "tls_http2"
VECTOR_IP_REPUTATION = "ip_reputation"
VECTOR_WAF_CHALLENGE = "waf_challenge"
VECTOR_MISC = "misc"


@dataclass(frozen=True)
class ProbeTarget:
    """
    Public diagnostic targets for validating our access patterns.

    This registry is intentionally limited to *measurement* targets. It should
    not be used for automated challenge solving or bypass workflows.
    """

    id: str
    label: str
    url: str
    vector: str
    description: str = ""
    manual_interaction: bool = False
    recommended: bool = True
    # scored=False: target is included for observation but excluded from pass/fail scoring
    # (e.g. known-flaky endpoints, SSL noise, manual-only checks)
    scored: bool = True
    # optional=True: target is not in the recommended default set; run via --urls only
    optional: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


PROBE_TARGETS: List[ProbeTarget] = [
    ProbeTarget(
        id="about_blank",
        label="about:blank",
        url="about:blank",
        vector=VECTOR_MISC,
        description="Sanity check: driver boots and can render a blank document.",
        recommended=True,
    ),
    ProbeTarget(
        id="httpbin_headers_sec_ch",
        label="httpbin.dev headers + Client Hints",
        url="https://httpbin.dev/headers-sec-ch",
        vector=VECTOR_MISC,
        description="Echo request headers + UA-CH to validate client hints and header presence.",
        recommended=True,
    ),
    ProbeTarget(
        id="httpbin_headers",
        label="httpbin.dev headers",
        url="https://httpbin.dev/headers",
        vector=VECTOR_MISC,
        description="Echo request headers to validate basic header set.",
        recommended=False,
    ),
    ProbeTarget(
        id="httpbin_get",
        label="httpbin.dev get",
        url="https://httpbin.dev/get",
        vector=VECTOR_MISC,
        description="Generic request echo endpoint for quick inspection.",
        recommended=False,
    ),
    ProbeTarget(
        id="sannysoft_bot",
        label="Sannysoft bot test",
        url="https://bot.sannysoft.com/",
        vector=VECTOR_BROWSER_ENV,
        description="Baseline browser-automation fingerprint checks.",
        recommended=True,
    ),
    ProbeTarget(
        id="incolumitas_bot",
        label="Incolumitas bot detector",
        url="https://bot.incolumitas.com/",
        vector=VECTOR_BROWSER_ENV,
        description="Actively maintained behavioral bot detection. Runs a 15 s background script tracking cursor/scroll events; needs full wait for an accurate score.",
        recommended=True,
    ),
    ProbeTarget(
        id="areyouheadless",
        label="Are You Headless? (Antoine Vastel)",
        url="https://arh.antoinevastel.com/bots/areyouheadless",
        vector=VECTOR_BROWSER_ENV,
        description="Checks advanced DOM variables and Function.prototype.toString tampering introduced by stealth patches.",
        recommended=True,
    ),
    ProbeTarget(
        id="fingerprintjs_playground",
        label="FingerprintJS playground",
        url="https://fingerprintjs.github.io/fingerprintjs/",
        vector=VECTOR_BROWSER_ENV,
        description="Live FingerprintJS deployment. Compare visitorId between a normal browser and your automation context — a stable ID means canvas/WebGL/audio fingerprint is leaked.",
        recommended=True,
    ),
    ProbeTarget(
        id="creepjs",
        label="CreepJS",
        url="https://creepjs.org/checker",
        vector=VECTOR_BROWSER_ENV,
        description="Comprehensive browser-modification detector. Tracks lies counter and trust score. PerimeterX relies on its lie-detection logic to issue 'Press and Hold' challenges.",
        recommended=True,
    ),
    ProbeTarget(
        id="scrapfly_browser_fingerprint",
        label="Scrapfly browser fingerprint diagnostics",
        url="https://scrapfly.io/web-scraping-tools/browser-fingerprint",
        vector=VECTOR_BROWSER_ENV,
        description="Deep fingerprint/leak diagnostics page (manual review).",
        manual_interaction=True,
        recommended=True,
    ),
    ProbeTarget(
        id="apivoid_bot_detection",
        label="APIVoid bot detection test",
        url="https://www.apivoid.com/tools/bot-detection-test/",
        vector=VECTOR_BROWSER_ENV,
        description="Client-side automation/leak checks with a risk score.",
        recommended=False,
    ),
    ProbeTarget(
        id="pixelscan_bot_checker",
        label="Pixelscan bot checker",
        url="https://pixelscan.net/bot-check",
        vector=VECTOR_BROWSER_ENV,
        description=(
            "Transport-architecture probe (CDP tab always detects chromedriver — structural, "
            "not fixable by stealth). Included for observability; excluded from pass/fail scoring."
        ),
        recommended=False,
        scored=False,
    ),
    ProbeTarget(
        id="amiunique",
        label="Am I Unique",
        url="https://amiunique.org/fingerprint",
        vector=VECTOR_BROWSER_ENV,
        description="Fingerprint uniqueness survey (large attribute surface).",
        recommended=True,
    ),
    ProbeTarget(
        id="tls_peet_all",
        label="tls.peet.ws (JA3/JA4 + HTTP/2) API",
        url="https://tls.peet.ws/api/all",
        vector=VECTOR_TLS_HTTP2,
        description="JA4/HTTP2 fingerprint snapshot as seen server-side.",
        recommended=True,
    ),
    ProbeTarget(
        id="tls_browserleaks_json",
        label="BrowserLeaks TLS JSON",
        url="https://tls.browserleaks.com/json",
        vector=VECTOR_TLS_HTTP2,
        description="TLS/JA4 JSON snapshot from BrowserLeaks.",
        recommended=False,
    ),
    ProbeTarget(
        id="cloudflare_quic",
        label="Cloudflare QUIC / HTTP version check",
        url="https://cloudflare-quic.com/",
        vector=VECTOR_TLS_HTTP2,
        description="Cloudflare-hosted page showing negotiated HTTP version and related protocol signals.",
        recommended=True,
    ),
    ProbeTarget(
        id="ipinfo_json",
        label="ipinfo.io JSON",
        url="https://ipinfo.io/json",
        vector=VECTOR_IP_REPUTATION,
        description="Public IP + ASN/org classification (basic reputation proxy).",
        recommended=True,
    ),
    ProbeTarget(
        id="scamalytics_ip",
        label="Scamalytics IP fraud check",
        url="https://scamalytics.com/ip",
        vector=VECTOR_IP_REPUTATION,
        description="IP fraud score lookup. The probe runner auto-appends the egress IP (learned from ipinfo.io) to the URL path so the result page loads directly.",
        recommended=True,
    ),
    ProbeTarget(
        id="ipqualityscore_lookup",
        label="IPQualityScore proxy/VPN lookup",
        url="https://www.ipqualityscore.com/free-ip-lookup-proxy-vpn-test/lookup/",
        vector=VECTOR_IP_REPUTATION,
        description="Interactive IP reputation + proxy/VPN indicator lookup.",
        manual_interaction=True,
        recommended=True,
    ),

    ProbeTarget(
        id="cloudflare_trace",
        label="Cloudflare trace",
        url="https://www.cloudflare.com/cdn-cgi/trace",
        vector=VECTOR_MISC,
        description="Plain-text trace endpoint (useful for quick egress/IP/colo sanity checks).",
        recommended=True,
    ),
    ProbeTarget(
        id="humansecurity_home",
        label="HUMAN Security (vendor page)",
        url="https://www.humansecurity.com/",
        vector=VECTOR_WAF_CHALLENGE,
        description="Vendor marketing surface that often includes bot-management scripts; treat as a soft-signal page.",
        recommended=False,
    ),
    ProbeTarget(
        id="hcaptcha_demo",
        label="hCaptcha demo",
        url="https://dashboard.hcaptcha.com/demo",
        vector=VECTOR_WAF_CHALLENGE,
        description="Interactive hCaptcha demo (manual interaction).",
        manual_interaction=True,
        recommended=False,
    ),
    ProbeTarget(
        id="recaptcha_demo",
        label="reCAPTCHA demo",
        url="https://www.google.com/recaptcha/api2/demo",
        vector=VECTOR_WAF_CHALLENGE,
        description="Interactive reCAPTCHA demo (manual interaction).",
        manual_interaction=True,
        recommended=False,
    ),
    ProbeTarget(
        id="cloudflare_turnstile_demo",
        label="Cloudflare Turnstile demo",
        url="https://turnstile.pages.dev/",
        vector=VECTOR_WAF_CHALLENGE,
        description="Official Cloudflare-hosted Turnstile demo — same non-interactive + interactive challenge stack deployed on Realtor and various search engines.",
        recommended=True,
    ),
    ProbeTarget(
        id="walgreens_perimeterx",
        label="Walgreens (PerimeterX / HUMAN)",
        url="https://www.walgreens.com/",
        vector=VECTOR_WAF_CHALLENGE,
        description="Enterprise PerimeterX instance — same stack as Zillow. Check cookie jar for _px3/_pxvid after the request; do not rely on page content.",
        recommended=True,
    ),
    ProbeTarget(
        id="fiverr_perimeterx",
        label="Fiverr (PerimeterX / HUMAN)",
        url="https://www.fiverr.com/",
        vector=VECTOR_WAF_CHALLENGE,
        description="Second enterprise PerimeterX sandbox. Cross-verify _px3 between Walgreens and Fiverr to isolate site-specific config from fingerprint issues.",
        recommended=True,
    ),
    ProbeTarget(
        id="nowsecure",
        label="nowsecure.nl challenge testbed",
        url="https://nowsecure.nl/",
        vector=VECTOR_WAF_CHALLENGE,
        description="Known Cloudflare challenge testbed. Intermittent SSL errors unrelated to stealth — use for observation only.",
        manual_interaction=True,
        recommended=False,
        scored=False,
        optional=True,
    ),
]


def probe_targets() -> List[Dict[str, Any]]:
    return [target.to_dict() for target in PROBE_TARGETS]


def recommended_probe_urls() -> List[str]:
    return [target.url for target in PROBE_TARGETS if target.recommended]


def scored_probe_urls() -> List[str]:
    """URLs that count toward pass/fail scoring (excludes optional/noisy targets)."""
    return [target.url for target in PROBE_TARGETS if target.scored]


def url_to_target_map() -> Dict[str, Any]:
    """Return {url: ProbeTarget} for fast lookup during scoring."""
    return {target.url: target for target in PROBE_TARGETS}
