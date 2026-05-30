import argparse
import ipaddress
import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from datetime import datetime, timezone

from re_analyzer.scrapers.page_diagnostics import detect_challenge, save_page_diagnostics, wait_for_manual_challenge
from re_analyzer.scrapers.browser_diagnostics import collect_browser_report
from re_analyzer.scrapers.human_input import HumanMouse
from re_analyzer.scrapers.scraping_utility import (
    CHROME_BINARY_EXECUTABLE_PATH,
    CHROMEDRIVER_EXECUTABLE_PATH,
    DriverConfig,
    get_selenium_driver,
)
from re_analyzer.utility.utility import DATA_PATH

# Set by SIGTERM handler so run_probe() can break out of the URL loop cleanly
# and main() still prints whatever partial results were collected.
_stop_flag = threading.Event()


def _sigterm_handler(signum, frame):
    print("[probe] SIGTERM received — stopping after current URL", flush=True)
    _stop_flag.set()


if sys.platform != "win32":
    signal.signal(signal.SIGTERM, _sigterm_handler)

DEFAULT_TEST_URLS = [
    "about:blank",
    # Layer 1 — client heuristics / automation leak
    "https://bot.incolumitas.com/",
    "https://arh.antoinevastel.com/bots/areyouheadless",
    "https://fingerprintjs.github.io/fingerprintjs/",
    "https://bot.sannysoft.com/",
    # Layer 2 — advanced client-side interrogation
    "https://creepjs.org/checker",
    "https://amiunique.org/fingerprint",
    # Layer 3 — network / TLS handshake (pre-JS)
    "https://tls.peet.ws/api/all",
    "https://httpbin.dev/headers-sec-ch",
    # Layer 4 — live WAF sandboxes
    "https://turnstile.pages.dev/",
    "https://www.walgreens.com/",
    "https://www.fiverr.com/",
    # Supplemental
    "https://cloudflare-quic.com/",
    "https://ipinfo.io/json",
    # Prefer Cloudflare trace over the marketing homepage (more stable + easier to parse).
    "https://www.cloudflare.com/cdn-cgi/trace",

    "https://dashboard.hcaptcha.com/demo",
    "https://www.google.com/recaptcha/api2/demo",
    "https://www.ipqualityscore.com/free-ip-lookup-proxy-vpn-test/lookup/",
]

# Additional wait beyond the base 3 s for sites that render asynchronously.
_EXTRA_WAIT_SECONDS: dict = {
    "bot.incolumitas.com": 15,   # behavioral classification needs ~15 s
    "creepjs.org": 7,            # CreepJS renders asynchronously
    "cheiron.org/creepjs": 7,    # legacy mirror (may be offline)
    "fingerprintjs.github.io": 6,  # FingerprintJS computes entropy components async
    "bot.sannysoft.com": 5,        # fpscanner table is appended after async fingerprint collection
    "amiunique.org": 8,            # large Vue app; allow tables/chips to render
    "scrapfly.io": 7,              # large diagnostics page; allow async sections to populate
    "pixelscan.net": 7,            # dynamic bot-checker UI
    "apivoid.com": 6,              # async rendering + third-party scripts
    "nowsecure.nl": 6,             # Cloudflare challenge testbed may redirect/settle
    "ipqualityscore.com": 6,       # dynamic rendering / async lookups
    "scamalytics.com": 4,          # dynamic rendering + Cloudflare JS challenge may fire
    "walgreens.com": 5,            # passive PerimeterX telemetry/cookie settling
    "fiverr.com": 5,               # passive PerimeterX telemetry/cookie settling
}

def _truncate_text(value, limit=220) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _status_from_label(label: str | None) -> str | None:
    text = str(label or "").strip().lower()
    if text in {"ok", "passed", "pass", "true"}:
        return "passed"
    if text in {"warn", "warning"}:
        return "warn"
    if text in {"fail", "failed", "false"}:
        return "failed"
    return None


def _status_from_css_class(class_name: str | None) -> str | None:
    text = str(class_name or "").lower()
    if "passed" in text:
        return "passed"
    if "failed" in text:
        return "failed"
    if "warn" in text or "warning" in text:
        return "warn"
    return None


def _signal(value, status: str | None = None):
    return {"value": _truncate_text(value), "status": status}


# ─── Per-site signal extractors ───────────────────────────────────────────────

def _extract_sannysoft(driver) -> dict | None:
    """Extract pass/fail/warn rows from the bot.sannysoft.com result tables."""
    script = r"""
    const mapping = {
        user_agent:        'user-agent-result',
        webdriver:         'webdriver-result',
        webdriver_advanced:'advanced-webdriver-result',
        chrome:            'chrome-result',
        permissions:       'permissions-result',
        plugins_length:    'plugins-length-result',
        plugins_type:      'plugins-type-result',
        languages:         'languages-result',
        webgl_vendor:      'webgl-vendor',
        webgl_renderer:    'webgl-renderer',
        broken_image:      'broken-image-dimensions'
    };
    const out = {};
    const statusFromClass = (cls) => {
      const text = String(cls || '').toLowerCase();
      if (text.includes('passed')) return 'passed';
      if (text.includes('failed')) return 'failed';
      if (text.includes('warn')) return 'warn';
      return null;
    };
    const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
    for (const [key, id] of Object.entries(mapping)) {
        const el = document.getElementById(id);
        if (!el) continue;
        const value = textOf(el);
        out[key] = { value, status: statusFromClass(el.className || '') };
    }

    // Fingerprint Scanner tests (fpscanner): dynamic table rows appended after fpCollect resolves.
    // We surface each test name (e.g. PHANTOM_UA, SELENIUM_DRIVER) as a parsed signal key so the
    // UI can highlight failures without trying to "fix" them automatically.
    try {
      const rows = Array.from(document.querySelectorAll('#fp2 tr'));
      for (const row of rows) {
        const cells = row.querySelectorAll('td');
        if (!cells || cells.length < 2) continue;
        const name = textOf(cells[0]);
        const status = statusFromClass(cells[1].className || '') || null;
        if (!name) continue;
        // Keep the value short: use the status label + a small JSON excerpt if present.
        const label = textOf(cells[1]) || '';
        const pre = cells.length >= 3 ? cells[2].querySelector('pre') : null;
        const payload = textOf(pre);
        const value = payload ? payload.slice(0, 220) : label;
        out[name] = { value, status };
      }
    } catch (e) {}

    // Canvas hashes: these render as "Hash: <int>" inside the canvas cell.
    try {
      ['canvas1','canvas2','canvas3','canvas4','canvas5'].forEach((id) => {
        const el = document.getElementById(id);
        if (!el) return;
        const text = textOf(el);
        const match = text.match(/Hash:\s*([\\-\\d]+)/i);
        if (match) out[id + '_hash'] = { value: match[1], status: null };
      });
    } catch (e) {}

    // "Some details" table is stable and gives a quick sanity snapshot. Keys are preserved verbatim.
    try {
      const headers = Array.from(document.querySelectorAll('h1'));
      const detailsHeader = headers.find(h => /some details/i.test(textOf(h)));
      if (detailsHeader) {
        let node = detailsHeader.nextElementSibling;
        while (node && node.tagName !== 'TABLE') node = node.nextElementSibling;
        if (node && node.tagName === 'TABLE') {
          const rows = Array.from(node.querySelectorAll('tr'));
          rows.forEach((tr) => {
            const tds = tr.querySelectorAll('td');
            if (!tds || tds.length < 2) return;
            const k = textOf(tds[0]);
            const v = textOf(tds[1]);
            if (k && v) out['detail.' + k] = { value: v.slice(0, 220), status: null };
          });
        }
      }
    } catch (e) {}

    return JSON.stringify(out);
    """
    try:
        raw = driver.execute_script(script)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _extract_areyouheadless(driver) -> dict | None:
    """Extract the verdict string from arh.antoinevastel.com/bots/areyouheadless."""
    try:
        text = driver.execute_script(
            "return document.body ? (document.body.innerText || document.body.textContent || '').trim() : '';"
        ) or ""
        if "You are not headless" in text:
            return {"response_text": {"value": "You are not headless", "status": "passed"}}
        if "You are headless" in text:
            return {"response_text": {"value": "You are headless", "status": "failed"}}
        excerpt = text[:300].strip()
        return {"response_text": {"value": excerpt or "(no text)", "status": None}} if excerpt else None
    except Exception:
        return None


def _simulate_incolumitas_behavioral(driver, duration_seconds: float = 13.0) -> None:
    """
    Inject natural-looking mouse and scroll events during the Incolumitas
    15-second behavioral scoring window using HumanMouse.

    Without any interaction the behavioral score element stays at "..." (no data)
    because the page has nothing to classify.  HumanMouse.wander() produces
    WindMouse movement across viewport waypoints plus reading-style burst scrolls
    for the full duration.
    """
    try:
        mouse = HumanMouse(driver)
        mouse.wander(
            duration=duration_seconds,
            scroll_probability=0.35,
            scroll_distance_range=(60, 200),
        )
    except Exception:
        pass


def _extract_incolumitas(driver) -> dict | None:
    """
    Extract bot.incolumitas.com diagnostics.

    Notes:
    - This is intentionally *measurement-only*. We do not attempt to solve the
      interactive "bot challenge" form or any CAPTCHAs.
    - Most signals are written into <pre> blocks by the page's own scripts, so
      we read those preformatted JSON blobs once rendering completes.
    """
    try:
        payload = driver.execute_script(
            r"""
            const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
            const navSnap = () => {
              try {
                return {
                  userAgent: navigator.userAgent || '',
                  appVersion: navigator.appVersion || '',
                  platform: navigator.platform || '',
                  deviceMemory: navigator.deviceMemory,
                  hardwareConcurrency: navigator.hardwareConcurrency,
                  language: navigator.language || '',
                  languages: Array.from(navigator.languages || []),
                };
              } catch (e) {
                return null;
              }
            };
            return {
              behavioralScoreText: textOf(document.getElementById('behavioralScore')),
              newTestsText: textOf(document.getElementById('new-tests')),
              oldTestsText: textOf(document.getElementById('detection-tests')),
              ipApiText: textOf(document.getElementById('ip-api-data')),
              httpHeadersText: textOf(document.getElementById('httpHeaders')),
              tcpipText: textOf(document.getElementById('p0f')),
              tlsFingerprintText: textOf(document.getElementById('tls-fingerprint')),
              fpjsText: textOf(document.getElementById('fpjs')),
              canvasFingerprintText: textOf(document.getElementById('canvas_fingerprint')),
              webglFingerprintText: textOf(document.getElementById('webgl_fingerprint')),
              webWorkerText: textOf(document.getElementById('webWorkerRes')),
              serviceWorkerText: textOf(document.getElementById('serviceWorkerRes')),
              fpCollectText: textOf(document.getElementById('fp')),
              mainNavigator: navSnap(),
            };
            """
        ) or {}
    except Exception:
        return None

    out: dict = {}
    main_nav = (payload or {}).get("mainNavigator") if isinstance((payload or {}).get("mainNavigator"), dict) else {}

    # Behavioral score renders as a number string (or "..." while the server is processing).
    score_text = str((payload or {}).get("behavioralScoreText") or "").strip()
    if score_text:
        if score_text == "...":
            # Server did not return a classification in the allotted window — label as
            # incomplete rather than a stealth failure; a non-response is not evidence of
            # bot detection.
            out["behavioralClassificationScore"] = _signal("(server pending — score stayed at '...')", None)
            out["behavioralClassificationScore_incomplete"] = _signal("true", "warn")
        else:
            try:
                score = float(score_text)
                out["behavioralClassificationScore"] = _signal(score_text, "passed" if score >= 0.75 else "failed")
            except ValueError:
                out["behavioralClassificationScore"] = _signal(score_text, None)
    else:
        out["behavioralClassificationScore"] = _signal("(element not found)", "warn")

    def _parse_json_blob(key: str) -> dict | None:
        raw = str((payload or {}).get(key) or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Sometimes whitespace/newlines end up in innerHTML; normalize and retry once.
            compact = raw.strip()
            try:
                return json.loads(compact)
            except Exception:
                return None

    def _parse_json_text(raw: str | None):
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None

    new_tests = _parse_json_blob("newTestsText") or {}
    if isinstance(new_tests, dict) and new_tests:
        failed = []
        warned = []
        for test_name, verdict in new_tests.items():
            status = _status_from_label(verdict)
            out[str(test_name)] = _signal(verdict, status)
            if status == "failed":
                failed.append(str(test_name))
            elif status == "warn":
                warned.append(str(test_name))
        out["new_tests_fail_count"] = _signal(str(len(failed)), "failed" if failed else "passed")
        if warned:
            out["new_tests_warn_count"] = _signal(str(len(warned)), "warn")
        if failed:
            out["new_tests_failed_keys"] = _signal(", ".join(sorted(failed))[:600], "failed")

    old_tests = _parse_json_blob("oldTestsText") or {}
    if isinstance(old_tests, dict) and old_tests:
        intoli = old_tests.get("intoli") if isinstance(old_tests.get("intoli"), dict) else {}
        if isinstance(intoli, dict):
            intoli_failed = []
            for test_name, verdict in intoli.items():
                status = _status_from_label(verdict)
                out[f"intoli.{test_name}"] = _signal(verdict, status)
                if status == "failed":
                    intoli_failed.append(str(test_name))
            if intoli_failed:
                out["intoli.fail_count"] = _signal(str(len(intoli_failed)), "failed")

        fpscanner = old_tests.get("fpscanner") if isinstance(old_tests.get("fpscanner"), dict) else {}
        if isinstance(fpscanner, dict):
            fpscanner_failed = []
            fpscanner_warned = []
            for test_name, verdict in fpscanner.items():
                status = _status_from_label(verdict)
                out[str(test_name)] = _signal(verdict, status)
                if status == "failed":
                    fpscanner_failed.append(str(test_name))
                elif status == "warn":
                    fpscanner_warned.append(str(test_name))
            out["fpscanner.fail_count"] = _signal(str(len(fpscanner_failed)), "failed" if fpscanner_failed else "passed")
            out["fpscanner.failed_keys"] = _signal(", ".join(sorted(fpscanner_failed)) or "(empty)", "failed" if fpscanner_failed else "passed")
            if fpscanner_warned:
                out["fpscanner.warn_count"] = _signal(str(len(fpscanner_warned)), "warn")
                out["fpscanner.warned_keys"] = _signal(", ".join(sorted(fpscanner_warned)), "warn")

    ip_api = _parse_json_blob("ipApiText") or {}
    if isinstance(ip_api, dict) and ip_api:
        ip = ip_api.get("ip") or ""
        if ip:
            out["ip_api.ip"] = _signal(ip, None)
        for key in ("is_datacenter", "is_proxy", "is_vpn", "is_tor", "is_abuser"):
            if key in ip_api:
                value = bool(ip_api.get(key))
                # For diagnostics, treat "true" as a negative signal (warn/fail).
                status = "failed" if value else "passed"
                out[f"ip_api.{key}"] = _signal(str(value).lower(), status)

        asn = ip_api.get("asn") if isinstance(ip_api.get("asn"), dict) else {}
        if asn:
            if asn.get("asn") is not None:
                out["ip_api.asn"] = _signal(str(asn.get("asn")), None)
            if asn.get("org"):
                out["ip_api.asn_org"] = _signal(str(asn.get("org")), None)

    # HTTP headers (server-observed). This is useful for spotting UA / language
    # mismatches between network-layer headers and JS-accessible navigator values.
    http_headers = _parse_json_blob("httpHeadersText")
    if isinstance(http_headers, dict) and http_headers:
        ua = http_headers.get("user-agent") or http_headers.get("User-Agent") or ""
        if ua:
            out["http_headers.user_agent"] = _signal(ua, None)
        accept_lang = http_headers.get("accept-language") or http_headers.get("Accept-Language") or ""
        if accept_lang:
            out["http_headers.accept_language"] = _signal(accept_lang, None)
        # UA-CH is optional, but missing values can be a useful diagnostic signal.
        for key in ("sec-ch-ua", "sec-ch-ua-platform", "sec-ch-ua-mobile"):
            value = http_headers.get(key) or http_headers.get(key.title()) or http_headers.get(key.upper()) or ""
            if value:
                out[f"http_headers.{key}"] = _signal(value, "passed")
            else:
                out[f"http_headers.{key}"] = _signal("(missing)", "warn")

        # Common proxy headers (presence is suspicious, but not always malicious).
        for key in ("x-forwarded-for", "x-real-ip", "forwarded", "via"):
            value = http_headers.get(key) or http_headers.get(key.title()) or http_headers.get(key.upper()) or ""
            if value:
                out[f"http_headers.{key}"] = _signal(value, "warn")

    # TCP/IP fingerprint + TLS fingerprint blocks are loaded by the page via fetch
    # and may be unavailable if the upstream endpoint is down or its certificate
    # chain is invalid. We still capture whatever text we see for manual review.
    tcpip_raw = str((payload or {}).get("tcpipText") or "").strip()
    tcpip_json = _parse_json_text(tcpip_raw)
    if isinstance(tcpip_json, dict) and tcpip_json:
        os_guess = tcpip_json.get("os") or tcpip_json.get("os_name") or tcpip_json.get("best_guess") or ""
        if os_guess:
            out["tcpip.os_guess"] = _signal(os_guess, None)
        if tcpip_json.get("link"):
            out["tcpip.link"] = _signal(str(tcpip_json.get("link")), None)
    elif tcpip_raw:
        out["tcpip.raw"] = _signal(tcpip_raw, "warn")

    tls_raw = str((payload or {}).get("tlsFingerprintText") or "").strip()
    tls_json = _parse_json_text(tls_raw)
    if isinstance(tls_json, dict) and tls_json:
        for key in ("ja3_hash", "ja4", "peetprint_hash", "client_hello", "fingerprint"):
            if key in tls_json and tls_json.get(key) is not None:
                out[f"tls.{key}"] = _signal(str(tls_json.get(key)), None)
    elif tls_raw:
        out["tls.raw"] = _signal(tls_raw, "warn")

    fpjs = str((payload or {}).get("fpjsText") or "").strip()
    if fpjs:
        out["fpjs_visitorId"] = _signal(fpjs, None)

    canvas_fp = str((payload or {}).get("canvasFingerprintText") or "").strip()
    if canvas_fp:
        out["canvas_fingerprint"] = _signal(canvas_fp, None)

    webgl_fp = str((payload or {}).get("webglFingerprintText") or "").strip()
    if webgl_fp:
        out["webgl_fingerprint"] = _signal(webgl_fp, None)

    def _normalize_langs(value):
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value if item is not None]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return []

    def _truthy_any(value) -> bool:
        if isinstance(value, (list, tuple)):
            return any(bool(v) for v in value)
        if isinstance(value, dict):
            return any(bool(v) for v in value.values())
        return bool(value)

    def _emit_worker_snapshot(prefix: str, raw_text: str | None):
        text = str(raw_text or "").strip()
        if not text:
            return
        out[f"{prefix}.raw"] = _signal(text, None)
        snap = _parse_json_text(text)
        if not isinstance(snap, dict) or not snap:
            return

        # Expose the high-signal navigator surface.
        for key in ("userAgent", "appVersion", "platform", "language"):
            if snap.get(key) is not None:
                out[f"{prefix}.{key}"] = _signal(str(snap.get(key)), None)
        for key in ("deviceMemory", "hardwareConcurrency"):
            if snap.get(key) is not None:
                out[f"{prefix}.{key}"] = _signal(str(snap.get(key)), None)

        langs = _normalize_langs(snap.get("languages"))
        if langs:
            out[f"{prefix}.languages"] = _signal(", ".join(langs), None)

        # Consistency checks vs the main window navigator.
        if main_nav:
            try:
                ua_match = (str(snap.get("userAgent") or "") == str(main_nav.get("userAgent") or ""))
                out[f"{prefix}.ua_match"] = _signal(str(ua_match).lower(), "passed" if ua_match else "failed")
            except Exception:
                pass
            try:
                plat_match = (str(snap.get("platform") or "") == str(main_nav.get("platform") or ""))
                out[f"{prefix}.platform_match"] = _signal(str(plat_match).lower(), "passed" if plat_match else "failed")
            except Exception:
                pass
            try:
                mem_match = (snap.get("deviceMemory") == main_nav.get("deviceMemory"))
                if snap.get("deviceMemory") is not None and main_nav.get("deviceMemory") is not None:
                    out[f"{prefix}.deviceMemory_match"] = _signal(str(bool(mem_match)).lower(), "passed" if mem_match else "failed")
            except Exception:
                pass
            try:
                hc_match = (snap.get("hardwareConcurrency") == main_nav.get("hardwareConcurrency"))
                if snap.get("hardwareConcurrency") is not None and main_nav.get("hardwareConcurrency") is not None:
                    out[f"{prefix}.hardwareConcurrency_match"] = _signal(str(bool(hc_match)).lower(), "passed" if hc_match else "failed")
            except Exception:
                pass
            try:
                main_langs = _normalize_langs(main_nav.get("languages"))
                langs_match = (langs == main_langs) if langs and main_langs else None
                if langs_match is not None:
                    out[f"{prefix}.languages_match"] = _signal(
                        str(langs_match).lower(),
                        "passed" if langs_match else "failed",
                    )
            except Exception:
                pass

    # Worker/service-worker navigator snapshots are helpful when debugging inconsistent
    # navigator property plumbing. We parse + summarize, but still keep the raw JSON.
    _emit_worker_snapshot("web_worker", (payload or {}).get("webWorkerText"))
    _emit_worker_snapshot("service_worker", (payload or {}).get("serviceWorkerText"))

    # fpCollect blob can be huge (contains detailed fingerprint surface). We only
    # report a compact subset; the full JSON is already saved in probe diagnostics.
    fp_collect = str((payload or {}).get("fpCollectText") or "").strip()
    if fp_collect:
        out["fp_collect.present"] = _signal("true", None)
        fp_json = _parse_json_text(fp_collect)
        if isinstance(fp_json, dict) and fp_json:
            def _bool_signal(key: str, value, good_when_false=True):
                if value is None:
                    return
                truthy = bool(value)
                status = "failed" if truthy and good_when_false else ("passed" if truthy else "failed")
                if good_when_false:
                    status = "passed" if not truthy else "failed"
                out[key] = _signal(str(truthy).lower(), status)

            _bool_signal("fp_collect.webdriver", fp_json.get("webDriver"), good_when_false=True)
            if fp_json.get("webDriverValue") is not None:
                out["fp_collect.webdriverValue"] = _signal(str(bool(fp_json.get("webDriverValue"))).lower(), None)

            _bool_signal("fp_collect.nightmareJS", fp_json.get("nightmareJS"), good_when_false=True)
            _bool_signal("fp_collect.sequentum", fp_json.get("sequentum"), good_when_false=True)
            _bool_signal("fp_collect.debugTool", fp_json.get("debugTool"), good_when_false=True)

            phantom = fp_json.get("phantomJS")
            if phantom is not None:
                out["fp_collect.phantomJS_present"] = _signal(str(_truthy_any(phantom)).lower(), "failed" if _truthy_any(phantom) else "passed")
            selenium = fp_json.get("selenium")
            if selenium is not None:
                out["fp_collect.selenium_present"] = _signal(str(_truthy_any(selenium)).lower(), "failed" if _truthy_any(selenium) else "passed")

            errors = fp_json.get("errorsGenerated")
            if isinstance(errors, list):
                non_null = [e for e in errors if e]
                out["fp_collect.errorsGenerated_count"] = _signal(str(len(non_null)), None)

            res_overflow = fp_json.get("resOverflow")
            if isinstance(res_overflow, dict):
                if res_overflow.get("errorName"):
                    out["fp_collect.resOverflow.errorName"] = _signal(str(res_overflow.get("errorName")), None)
                if res_overflow.get("errorMessage"):
                    out["fp_collect.resOverflow.errorMessage"] = _signal(str(res_overflow.get("errorMessage")), None)

    return out or None


def _extract_fingerprintjs(driver) -> dict | None:
    """
    Extract FingerprintJS playground signals.

    We intentionally *summarize* the entropy components instead of returning the
    full components blob (which can include large base64 canvas payloads).
    """
    try:
        try:
            driver.set_script_timeout(30)
        except Exception:
            pass

        raw = driver.execute_async_script(
            r"""
            const done = arguments[arguments.length - 1];
            const trunc = (value, limit = 220) => {
              const text = String(value ?? '').trim();
              return text.length <= limit ? text : text.slice(0, limit - 3) + '...';
            };
            const hash32 = (text) => {
              // Stable, lightweight non-crypto hash for diagnostics (not security).
              const str = String(text ?? '');
              let h = 0;
              for (let i = 0; i < str.length; i += 1) {
                h = ((h << 5) - h) + str.charCodeAt(i);
                h |= 0;
              }
              return String(h);
            };
            const add = (out, key, value, status = null) => {
              out[key] = { value: trunc(value), status };
            };
            // Wait up to 10 s for window.FingerprintJS to appear (page may load it deferred).
            const waitForFP = () => new Promise((resolve) => {
              if (window.FingerprintJS && typeof window.FingerprintJS.load === 'function') {
                resolve(window.FingerprintJS);
                return;
              }
              let attempts = 0;
              const check = setInterval(() => {
                attempts++;
                if ((window.FingerprintJS && typeof window.FingerprintJS.load === 'function') || attempts >= 20) {
                  clearInterval(check);
                  resolve(window.FingerprintJS || null);
                }
              }, 500);
            });
            const domFallback = (out) => {
              // FP not globally accessible — extract whatever the page already rendered.
              const knownGlobals = ['FingerprintJS', 'fpjs', '__FP__', 'Fingerprint2', 'ClientJS', 'fpAgent'];
              const found = knownGlobals.filter(g => typeof window[g] !== 'undefined');
              const body = document.body ? (document.body.innerText || '') : '';
              // Look for a rendered hex visitor ID (FP IDs are 32 hex chars)
              const hexMatch = body.match(/\b([0-9a-f]{28,36})\b/i);
              const hasVisitorId = Boolean(hexMatch);
              // fp_api_missing: only warn when we also failed to extract a visitorId.
              // DOM fallback with a valid visitorId is an acceptable outcome — the
              // library is bundled as a module without a global, not a detection failure.
              add(out, 'fp_api_missing', 'true (window.FingerprintJS not found after 10s)', hasVisitorId ? null : 'warn');
              if (found.length) add(out, 'fp_globals_found', found.join(', '), 'warn');
              if (hexMatch) add(out, 'visitorId_dom_fallback', hexMatch[1], null);  // present = acceptable
              // Specific elements the playground may render
              const selectors = ['#result', '#visitor-id', '.visitor-id', '#fpjsOutput', '[data-visitor-id]', '.visitorId', '#fp-result'];
              for (const sel of selectors) {
                try {
                  const el = document.querySelector(sel);
                  if (el) add(out, 'dom_' + sel.replace(/[^\w]/g, '_'), (el.innerText || el.textContent || '').trim().slice(0, 80), null);
                } catch(e) {}
              }
              add(out, 'page_title', document.title || '', null);
              add(out, 'page_readyState', document.readyState || '', null);
            };
            (async () => {
              try {
                const FP = await waitForFP();
                if (!FP || typeof FP.load !== 'function') {
                  const out = {};
                  domFallback(out);
                  done(JSON.stringify(out));
                  return;
                }
                const agent = await FP.load();
                const result = await agent.get();
                const components = result && typeof result === 'object' ? (result.components || {}) : {};
                const out = {};

                add(out, 'visitorId', result.visitorId || '', null);
                const score = result?.confidence?.score;
                if (typeof score === 'number') {
                  add(out, 'confidence_score', String(score), score >= 0.7 ? 'passed' : 'warn');
                }
                if (result?.version) add(out, 'fp_version', result.version, null);

                const componentKeys = Object.keys(components || {}).sort();
                add(out, 'components_count', String(componentKeys.length), null);
                add(out, 'component_keys_hash32', hash32(componentKeys.join('|')), null);

                const componentErrorKeys = [];
                for (const k of componentKeys) {
                  const v = components[k];
                  if (v && typeof v === 'object' && v.error) componentErrorKeys.push(k);
                }
                add(out, 'components_error_count', String(componentErrorKeys.length), componentErrorKeys.length === 0 ? 'passed' : 'warn');
                if (componentErrorKeys.length) {
                  add(out, 'components_error_keys_sample', componentErrorKeys.slice(0, 12).join(', '), 'warn');
                }

                // "Are we checking everything?":
                // FingerprintJS exposes its collectors via the `components` map.
                // We don't dump raw component values (can be huge), but we *do*
                // surface per-component OK/error status so you can see if any
                // collector is missing or failing without scrolling HTML.
                let okCount = 0;
                for (const k of componentKeys) {
                  const v = components[k];
                  const hasValue = v && typeof v === 'object' && ('value' in v);
                  const hasError = v && typeof v === 'object' && ('error' in v);
                  if (hasValue && !hasError) okCount += 1;
                  // Keep this compact: status only, no raw values.
                  out[`component.${k}`] = { value: hasError ? 'error' : (hasValue ? 'ok' : 'missing'), status: hasError ? 'warn' : (hasValue ? 'passed' : 'warn') };
                }
                add(out, 'components_ok_count', String(okCount), okCount === componentKeys.length ? 'passed' : 'warn');

                const getValue = (name) => {
                  const entry = components?.[name];
                  if (!entry || typeof entry !== 'object') return null;
                  if ('value' in entry) return entry.value;
                  return null;
                };

                // A few high-signal components (summaries only).
                const ua = getValue('userAgent');
                if (ua) add(out, 'user_agent', ua, null);

                const uad = getValue('userAgentData');
                if (uad && typeof uad === 'object') {
                  const platform = uad.platform ?? '';
                  const arch = uad.architecture ?? '';
                  const bitness = uad.bitness ?? '';
                  const pv = uad.platformVersion ?? '';
                  add(out, 'ua_ch_platform', platform, platform ? 'passed' : 'warn');
                  add(out, 'ua_ch_architecture', arch, arch ? 'passed' : 'warn');
                  add(out, 'ua_ch_bitness', bitness, bitness ? 'passed' : 'warn');
                  add(out, 'ua_ch_platform_version', pv, pv ? 'passed' : 'warn');

                  // Platform mismatch between UA string + UA-CH is a high-signal
                  // automation anti-pattern. Note: macOS UA reduction intentionally
                  // freezes the OS version and can still include "Intel" on Apple
                  // Silicon machines; that case is not treated as a mismatch here.
                  const inferUaPlatform = (uaString) => {
                    const s = String(uaString || '');
                    if (s.includes('Windows')) return 'Windows';
                    if (s.includes('Android')) return 'Android';
                    if (s.includes('iPhone') || s.includes('iPad') || s.includes('iPod')) return 'iOS';
                    if (s.includes('Macintosh') || s.includes('Mac OS X')) return 'macOS';
                    if (s.includes('CrOS')) return 'Chrome OS';
                    if (s.includes('Linux')) return 'Linux';
                    return '';
                  };
                  const uaPlatform = inferUaPlatform(ua);
                  if (uaPlatform && platform) {
                    add(out, 'ua_platform_mismatch', String(uaPlatform !== platform), uaPlatform === platform ? 'passed' : 'failed');
                  }

                  // Helpful context: detect macOS UA reduction (Chrome caps macOS
                  // version in the UA string at 10_15_7 on modern macOS).
                  try {
                    const pvMajor = parseInt(String(pv).split('.')[0] || '', 10);
                    const uaHasFrozenMac = String(ua || '').includes('Mac OS X 10_15_7');
                    if (platform === 'macOS' && pvMajor && pvMajor >= 11 && uaHasFrozenMac) {
                      add(out, 'ua_reduction_detected', 'true', 'passed');
                    }
                  } catch (e) {}
                }

                const fonts = getValue('fonts');
                if (Array.isArray(fonts)) {
                  add(out, 'fonts_count', String(fonts.length), fonts.length >= 3 ? 'passed' : 'warn');
                  add(out, 'fonts_sample', fonts.slice(0, 6).join(', '), null);
                }

                const audio = getValue('audio');
                if (audio != null) add(out, 'audio_value', typeof audio === 'number' ? audio.toFixed(6) : String(audio), null);

                const screenFrame = getValue('screenFrame');
                if (Array.isArray(screenFrame)) add(out, 'screen_frame', JSON.stringify(screenFrame), null);

                const canvas = getValue('canvas');
                if (canvas && typeof canvas === 'object') {
                  if ('winding' in canvas) add(out, 'canvas_winding', String(Boolean(canvas.winding)), null);
                  const geom = canvas.geometry;
                  if (typeof geom === 'string' && geom) {
                    add(out, 'canvas_geometry_hash', hash32(geom), null);
                    add(out, 'canvas_geometry_len', String(geom.length), null);
                  }
                }

                done(JSON.stringify(out));
              } catch (e) {
                const errOut = {};
                add(errOut, 'fp_error', String(e).slice(0, 160), 'warn');
                domFallback(errOut);
                done(JSON.stringify(errOut));
              }
            })();
            """
        )
        return json.loads(raw) if raw else None
    except Exception as exc:
        return {"fp_script_exception": {"value": str(exc)[:160], "status": "warn"}}


def _extract_worker_isolation(driver) -> dict | None:
    """
    Spawn a transient Web Worker and compare its navigator properties to the main
    window.  Stealth plugins that only patch the main execution context leave the
    worker scope untouched — navigator.webdriver will be `true` there even if
    it is `undefined` in the window.  PerimeterX and Akamai both check this
    second-order leak channel.
    """
    script = r"""
    const done = arguments[arguments.length - 1];
    const workerSrc = `
        const snap = {};
        try { snap.webdriver = navigator.webdriver; } catch(e) { snap.webdriver_err = String(e); }
        try { snap.userAgent  = navigator.userAgent;  } catch(e) {}
        try { snap.platform   = navigator.platform;   } catch(e) {}
        try { snap.languages  = Array.from(navigator.languages || []); } catch(e) {}
        try { snap.hardwareConcurrency = navigator.hardwareConcurrency; } catch(e) {}
        self.postMessage(snap);
    `;
    try {
        const blob = new Blob([workerSrc], { type: 'application/javascript' });
        const url  = URL.createObjectURL(blob);
        const w    = new Worker(url);
        const tid  = setTimeout(() => { w.terminate(); URL.revokeObjectURL(url); done(null); }, 5000);
        w.onmessage = (e) => {
            clearTimeout(tid);
            w.terminate();
            URL.revokeObjectURL(url);
            const wd   = e.data || {};
            const main = {
                userAgent: navigator.userAgent || '',
                platform:  navigator.platform  || '',
                languages: Array.from(navigator.languages || []),
                hardwareConcurrency: navigator.hardwareConcurrency,
            };
            done(JSON.stringify({
                worker_webdriver:        wd.webdriver,
                worker_ua:               wd.userAgent || '',
                worker_platform:         wd.platform  || '',
                worker_languages:        wd.languages || [],
                worker_hw_concurrency:   wd.hardwareConcurrency,
                ua_match:                wd.userAgent  === main.userAgent,
                platform_match:          wd.platform   === main.platform,
                hw_concurrency_match:    wd.hardwareConcurrency === main.hardwareConcurrency,
            }));
        };
        w.onerror = () => { clearTimeout(tid); w.terminate(); URL.revokeObjectURL(url); done(null); };
    } catch(e) {
        done(null);
    }
    """
    try:
        try:
            driver.set_script_timeout(10)
        except Exception:
            pass
        raw = driver.execute_async_script(script)
        if not raw:
            return None
        data = json.loads(raw)
        out: dict = {}
        # Primary signal: navigator.webdriver inside the worker
        wd = data.get("worker_webdriver")
        out["worker.webdriver"] = _signal(
            str(wd) if wd is not None else "undefined",
            "passed" if not wd else "failed",
        )
        # Consistency across the worker boundary
        for key, label in (
            ("ua_match", "worker.ua_match"),
            ("platform_match", "worker.platform_match"),
            ("hw_concurrency_match", "worker.hw_concurrency_match"),
        ):
            val = bool(data.get(key))
            out[label] = _signal(str(val).lower(), "passed" if val else "failed")
        return out
    except Exception:
        return None


def _extract_creepjs(driver) -> dict | None:
    """
    Extract lies counter, trust score, and worker isolation signals from creepjs.

    CreepJS renders results asynchronously — wait for the extra seconds
    configured in _EXTRA_WAIT_SECONDS before this is called.
    Worker isolation is tested inline here because the CreepJS page context
    is already loaded and any active stealth overrides are in place.
    """
    script = r"""
    const out = {};
    const body = document.body;
    const allText = body ? (body.innerText || body.textContent || '') : '';

    const trunc = (value, limit = 220) => {
      const text = String(value ?? '').trim();
      return text.length <= limit ? text : text.slice(0, limit - 3) + '...';
    };

    const set = (k, value, status = null) => { out[k] = { value: trunc(value), status }; };

    // Helpful context: confirm we're on the expected page.
    let analyzingBanner = '';
    try {
      set('page_title', document.title || '', null);
      const h1 = document.querySelector('h1');
      if (h1 && (h1.innerText || h1.textContent)) set('h1', h1.innerText || h1.textContent, null);
      const h2s = Array.from(document.querySelectorAll('h2')).map(el => (el.innerText || el.textContent || '').trim()).filter(Boolean);
      analyzingBanner = h2s.find(t => /analyzing\s+your\s+browser/i.test(t)) || '';
    } catch (e) {}

    // Challenge evidence (Cloudflare / Turnstile / hCaptcha / reCAPTCHA)
    let hasChallenge = false;
    try {
      // Keep this conservative: do not flag generic captcha providers/widgets unless
      // they are clearly part of an interstitial challenge flow.
      hasChallenge = Boolean(
        document.querySelector(
          '#cf-challenge-running, #challenge-form, iframe[src*=\"challenges.cloudflare.com\"], input[name=\"cf_captcha_kind\"], input[name=\"cf_challenge_response\"]'
        ) ||
        /checking your browser before accessing|attention required|ddos protection by cloudflare|cf-chl-/i.test(allText)
      );
      if (hasChallenge) set('creepjs.challenge_like', 'true', 'warn');
    } catch (e) {}

    // 1) Text regexes (works on many older builds)
    const liesMatch = allText.match(/(\d+)\s*lie/i);
    if (liesMatch) {
      const n = parseInt(liesMatch[1], 10);
      set('lies', String(n), n === 0 ? 'passed' : 'failed');
    }

    const trustMatch = allText.match(/trust\b[^%\n]{0,80}?(\d{1,3}(?:\.\d+)?)\s*%/i)
                    || allText.match(/(\d{1,3}(?:\.\d+)?)\s*%[^\n]{0,80}?trust/i);
    if (trustMatch) {
      const pct = parseFloat(trustMatch[1]);
      set('trust_score', trustMatch[1] + '%', pct >= 85 ? 'passed' : 'failed');
    }

    // 2) DOM-based extraction (newer Tailwind/SPA layouts often separate label/value)
    const normalize = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');

    const findMetricValueNear = (labelRegex, valueRegex) => {
      const nodes = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,div,span,p,dt,dd,th,td,strong,b,code'));
      for (const node of nodes) {
        const label = textOf(node);
        if (!label) continue;
        if (!labelRegex.test(normalize(label))) continue;
        const container = node.closest('section,article,li,dl,div') || node.parentElement;
        const bucket = container ? Array.from(container.querySelectorAll('code,strong,b,span,div,p,dd,td')) : [];
        for (const el of bucket) {
          const t = textOf(el);
          if (!t) continue;
          const m = valueRegex ? t.match(valueRegex) : null;
          if (m) return m[0];
        }
        // fallback: try nearby siblings
        const sibs = [];
        if (node.parentElement) sibs.push(...Array.from(node.parentElement.querySelectorAll('code,strong,b,span,div,p,dd,td')));
        for (const el of sibs) {
          const t = textOf(el);
          if (!t) continue;
          const m = valueRegex ? t.match(valueRegex) : null;
          if (m) return m[0];
        }
      }
      return '';
    };

    if (!('trust_score' in out)) {
      const near = findMetricValueNear(/\btrust\b/, /(\d{1,3}(?:\.\d+)?)\s*%/);
      const m = near.match(/(\d{1,3}(?:\.\d+)?)\s*%/);
      if (m) {
        const pct = parseFloat(m[1]);
        set('trust_score', m[1] + '%', pct >= 85 ? 'passed' : 'failed');
      }
    }

    if (!('lies' in out)) {
      const near = findMetricValueNear(/\blie(s)?\b/, /(\d+)\s*(?:lie|lies)\b/i);
      const m = near.match(/(\d+)\s*(?:lie|lies)\b/i) || near.match(/\b(\d+)\b/);
      if (m) {
        const n = parseInt(m[1], 10);
        set('lies', String(n), n === 0 ? 'passed' : 'failed');
      }
    }

    // 3) Lie category names -- which specific browser surfaces were caught lying
    try {
      const lieCategories = new Set();
      // Pattern A: elements in explicit lie list containers
      document.querySelectorAll('[data-lies], .lies-list li, .lie-item, [class*="lie"] li').forEach(el => {
        const t = textOf(el).split('\n')[0].slice(0, 60);
        if (t) lieCategories.add(t);
      });
      // Pattern B: keyword-proximity scan when lie count > 0
      if (lieCategories.size === 0) {
        const lN = parseInt(((out.lies || {}).value || '0'), 10);
        if (lN > 0) {
          Array.from(document.querySelectorAll('li,dt,th,td,code,strong'))
            .filter(el => /function\.prototype|navigator|screen|canvas|webgl|audio|permissions|worker|toString|getOwnProperty/i.test(textOf(el)))
            .slice(0, 12)
            .forEach(el => { const t = textOf(el).split('\n')[0].slice(0, 80); if (t) lieCategories.add(t); });
        }
      }
      if (lieCategories.size) set('lies_categories', Array.from(lieCategories).join(' | '), 'failed');
    } catch(e) {}

    // 4) Section-level signals: permissions, screen, audio, canvas, WebGL, fonts, timezone
    try {
      const sectionDefs = [
        ['permissions', /\bpermissions?\b/i],
        ['screen', /\bscreen\b/i],
        ['audio', /\baudio\b/i],
        ['canvas', /\bcanvas\b/i],
        ['webgl', /\bwebgl\b/i],
        ['fonts', /\bfonts?\b/i],
        ['timezone', /\btimezone\b|\btime\s*zone\b/i],
        ['prototype', /\bprototype\b|\bproto\b/i],
      ];
      const sectionEls = Array.from(document.querySelectorAll('section, article, [class*="section"], details'));
      for (const [sectionKey, regex] of sectionDefs) {
        const el = sectionEls.find(e => regex.test(textOf(e.querySelector('h2,h3,h4,summary') || e).slice(0, 60)));
        if (!el) continue;
        const valueEl = el.querySelector('code, .value, strong, [class*="status"], [class*="result"]');
        const snippet = valueEl ? textOf(valueEl).slice(0, 120) : textOf(el).slice(0, 120).split('\n').slice(0, 2).join(' ');
        if (snippet) set('creepjs.section.' + sectionKey, snippet, null);
      }
    } catch(e) {}

    // 5) Trash / entropy / prototype tampering evidence
    try {
      const allText2 = allText.slice(0, 8000);
      const trashMatch = allText2.match(/trash\s*:\s*(\d+)/i) || allText2.match(/(\d+)\s*trash/i);
      if (trashMatch) set('creepjs.trash', trashMatch[1], Number(trashMatch[1]) > 0 ? 'failed' : 'passed');
      const entropyMatch = allText2.match(/entropy\s*:\s*([\d.]+)/i);
      if (entropyMatch) set('creepjs.entropy', entropyMatch[1], null);
      const protoPatterns = [/toString\s*:\s*(native|tamper|lie|modified)/i, /getOwnProperty.*?(tamper|modified|lie)/i];
      for (const p of protoPatterns) {
        const m = allText2.match(p);
        if (m) { set('creepjs.proto_tamper_evidence', m[0].slice(0, 80), 'failed'); break; }
      }
    } catch(e) {}

    // If we could not find key metrics, surface that so the UI does not show
    // a misleading "passed" based on browser hygiene signals alone.
    try {
      const hasKeyMetrics = Boolean(out.lies || out.trust_score);
      if (analyzingBanner && !hasKeyMetrics && !hasChallenge) set('analyzing_banner', analyzingBanner, 'warn');
      if (!hasKeyMetrics) set('creepjs.metrics_missing', 'true', 'warn');
    } catch (e) {}

    return Object.keys(out).length ? JSON.stringify(out) : null;
    """
    try:
        raw = driver.execute_script(script)
        out = json.loads(raw) if raw else {}
    except Exception:
        out = {}

    # Worker isolation — spawned while CreepJS stealth overrides are active
    try:
        worker_signals = _extract_worker_isolation(driver)
        if worker_signals:
            out.update(worker_signals)
    except Exception:
        pass

    return out or None


def _extract_pixelscan(driver) -> dict | None:
    """
    Best-effort parsing for Pixelscan bot-check pages.

    Extracts tab-level summaries AND per-subtest detail rows so the UI can
    show exactly which CDP / navigator / header subtests are flagged and why.
    """
    script = r"""
    const trunc = (value, limit = 300) => {
      const text = String(value ?? '').trim();
      return text.length <= limit ? text : text.slice(0, limit - 3) + '...';
    };
    const out = {};
    const set = (k, value, status = null) => { out[k] = { value: trunc(value), status }; };
    const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');

    const isDetected = (s) => /detected|fail|inconsistent|mismatch|blocked/i.test(s);
    const isClear = (s) => /clear|ok|pass|success|none/i.test(s);
    const rowStatus = (s) => isClear(s) ? 'passed' : isDetected(s) ? 'failed' : 'warn';

    try {
      set('page_title', document.title || '', null);
      const h1 = document.querySelector('h1');
      if (h1) set('h1', textOf(h1), null);
    } catch (e) {}

    // Header state + title
    try {
      const header = document.querySelector('pxlscn-checker-header');
      const headerRoot = header || document.querySelector('.checker-header');
      const checker = headerRoot ? (headerRoot.querySelector('.checker-header') || headerRoot) : null;
      const state = checker ? (checker.getAttribute('state') || '') : '';
      if (state) {
        const status = (state === 'success') ? 'passed' : ((state === 'error') ? 'failed' : 'warn');
        set('pixelscan.state', state, status);
      }
      const headerH2 = checker ? checker.querySelector('h2') : null;
      if (headerH2) set('pixelscan.header_title', textOf(headerH2), null);
      // Reason/explanation beneath the header (e.g. "we detected use of DevTools")
      const headerDesc = checker ? checker.querySelector('p, .description, .subtitle') : null;
      if (headerDesc) set('pixelscan.header_reason', textOf(headerDesc), null);
    } catch (e) {}

    // Error explanation banner
    try {
      const failed = document.querySelector('.failed-bot-check');
      if (failed) {
        const desc = failed.querySelector('.failed-bot-desc');
        if (desc) set('pixelscan.failed_reason', textOf(desc), 'failed');
        // Capture all child paragraphs as extra context
        Array.from(failed.querySelectorAll('p')).forEach((p, i) => {
          const t = textOf(p);
          if (t && i < 4) set(`pixelscan.failed_detail_${i}`, t, 'failed');
        });
      }
    } catch (e) {}

    // Tabs summary + per-tab panel detail
    try {
      const tabs = document.querySelector('#tabs[role=\"tablist\"], [role=\"tablist\"]#tabs, .bot-check-summary__tabs');
      if (tabs) {
        const buttons = Array.from(tabs.querySelectorAll('button[role=\"tab\"]'));
        set('pixelscan.tabs_count', String(buttons.length), null);
        for (const btn of buttons) {
          const id = btn.getAttribute('id') || '';
          const section = btn.querySelector('.summary-section');
          const title = section ? textOf(section.querySelector('.summary-section__title')) : '';
          const statusText = section ? textOf(section.querySelector('.summary-section__status')) : '';
          const countText = section ? textOf(section.querySelector('.summary-section__count')) : '';
          const tabKey = (id || title || 'tab').replace(/[^a-zA-Z0-9_.-]/g, '_').toLowerCase();
          if (title) set(`pixelscan.tab.${tabKey}.title`, title, null);
          if (statusText) {
            // CDP detection is structural in any Selenium/chromedriver session — the
            // DevTools protocol channel is always visible.  Downgrade from failed→warn
            // so it does not count as a stealth regression; add a note for context.
            const isCdpSection = /\bcdp\b/i.test(tabKey);
            const tabSt = (isCdpSection && rowStatus(statusText) === 'failed') ? 'warn' : rowStatus(statusText);
            set(`pixelscan.tab.${tabKey}.status`, statusText, tabSt);
            if (isCdpSection && /detected/i.test(statusText)) {
              set('pixelscan.cdp_structural_note',
                'CDP is always Detected in Selenium/chromedriver — not fixable by stealth patching alone; ' +
                'compare with a DevTools-open/closed baseline to distinguish structural from contextual signal',
                null);
            }
          }
          if (countText) set(`pixelscan.tab.${tabKey}.count`, countText, null);

          // Find the corresponding panel: aria-controls attr → getElementById,
          // or fallback to adjacent/sibling panel elements.
          const panelId = btn.getAttribute('aria-controls') || '';
          const panel = panelId ? document.getElementById(panelId) : null;
          const panelEl = panel
            || document.querySelector(`[id="${tabKey}"], [data-tab="${tabKey}"], [role="tabpanel"][aria-labelledby="${id}"]`)
            || null;

          if (panelEl) {
            // Row-level extraction: try multiple selector patterns used by Pixelscan
            const rowSelectors = [
              '.check-item', '.test-row', '.bot-check-item',
              'li[class*="check"]', 'tr[class*="check"]',
              '.check-details__item', '.panel-item', '.result-row',
            ];
            let rows = [];
            for (const sel of rowSelectors) {
              rows = Array.from(panelEl.querySelectorAll(sel));
              if (rows.length) break;
            }
            // Generic fallback: any <li> or <tr> inside the panel
            if (!rows.length) rows = Array.from(panelEl.querySelectorAll('li, tr')).slice(0, 30);

            rows.forEach((row, ri) => {
              if (ri >= 30) return;
              // Try labelled name + status columns
              const nameEl = row.querySelector('.check-name, .name, .label, td:first-child, .item-name, .key') || row;
              const statusEl = row.querySelector('.check-status, .status, td:last-child, .result, .value, .verdict');
              const name = textOf(nameEl !== row ? nameEl : null) || textOf(row).split('\n')[0];
              const status = statusEl ? textOf(statusEl) : '';
              if (!name || name.length > 120) return;
              const rowKey = name.replace(/[^a-zA-Z0-9_.-]/g, '_').toLowerCase().slice(0, 60);
              if (!rowKey) return;
              const sig = status || 'present';
              set(`pixelscan.tab.${tabKey}.item.${rowKey}`, sig, rowStatus(sig));
            });

            // Surface "inconsistent" labels anywhere in the panel
            const inconsistentEls = Array.from(panelEl.querySelectorAll('[class*="inconsistent"], [data-status*="inconsistent"], .mismatch'));
            inconsistentEls.forEach((el, i) => {
              if (i >= 5) return;
              set(`pixelscan.tab.${tabKey}.inconsistent_${i}`, textOf(el), 'failed');
            });
          }
        }
      }
    } catch (e) {}

    // Top-level "bot check" table that some Pixelscan layouts render outside tabs
    try {
      const table = document.querySelector('table.bot-check-table, table.check-results, table');
      if (table) {
        const rows = Array.from(table.querySelectorAll('tr')).slice(0, 40);
        rows.forEach((row, ri) => {
          const cells = Array.from(row.querySelectorAll('td,th'));
          if (cells.length < 2) return;
          const name = textOf(cells[0]);
          const result = textOf(cells[cells.length - 1]);
          if (!name || name.length > 100) return;
          const k = name.replace(/[^a-zA-Z0-9_.-]/g, '_').toLowerCase().slice(0, 60);
          if (k) set(`pixelscan.table.${k}`, result, rowStatus(result));
        });
      }
    } catch (e) {}

    return Object.keys(out).length ? JSON.stringify(out) : null;
    """
    try:
        raw = driver.execute_script(script)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _summarize_cookie(cookie: dict | None) -> dict:
    if not cookie:
        return {"present": False, "value": "(absent)", "length": 0, "expiry": None}
    value = str(cookie.get("value") or "")
    return {
        "present": bool(value),
        "value": value[:80] if value else "(empty)",
        "length": len(value),
        "expiry": cookie.get("expiry"),
        "domain": cookie.get("domain"),
    }


def _extract_perimeter_x_cookies(driver) -> dict | None:
    """
    Check PerimeterX / HUMAN state.

    Cookie presence alone is not enough: challenge pages can set _px3-like
    values while still rendering a PXCR/Press-and-Hold block. Visible block
    evidence always overrides cookie evidence.

    Extended telemetry:
    - All _px* cookies (_px, _px3, _pxvid, _pxcts, _pxde, _pxhd, pxcts)
    - PX JS execution signals (window._pxAppId, _pxParam, _px_a_c)
    - Blocked/loaded PX resources via PerformanceResourceTiming
    - Challenge iframe URL detection
    - reCAPTCHA fallback detection within the challenge
    """
    # All known PerimeterX cookie names (primary + secondary)
    _PX_COOKIE_NAMES = ["_px3", "_pxvid", "_px", "_pxcts", "_pxde", "_pxhd", "_pxff", "_pxmvid", "pxcts"]

    try:
        settle_raw = str(os.environ.get("RE_ANALYZER_PX_SETTLE_SECONDS", "3") or "").strip()
        try:
            settle_seconds = max(0.0, float(settle_raw))
        except Exception:
            settle_seconds = 3.0
        try:
            text = driver.execute_script("return document.body ? (document.body.innerText || '') : '';") or ""
        except Exception:
            text = ""
        challenge_text = str(text or "")
        px_error_match = re.search(r"\bPXCR\d+\b", challenge_text, flags=re.IGNORECASE)
        has_px_block = bool(
            px_error_match
            or re.search(r"it needs a human touch|complete the task and we['']?ll get you right back|press\s*(?:&|and)\s*hold", challenge_text, flags=re.IGNORECASE)
        )

        # Passive observation: keep the page open so async PX scripts can set cookies.
        # RE_ANALYZER_PX_EXTENDED_TIMELINE=1 enables 0/3/10/30s timeline to capture
        # deferred trust-cookie issuance patterns.
        extended_timeline = os.environ.get("RE_ANALYZER_PX_EXTENDED_TIMELINE", "").strip().lower() in {"1", "true", "yes"}
        if extended_timeline:
            max_t = max(settle_seconds, 30.0)
            points = sorted({p for p in (0.0, 3.0, 10.0, 30.0) if p <= max_t})
        else:
            points = [0.0]
            if settle_seconds > 3.0:
                points.append(3.0)
            if settle_seconds not in points:
                points.append(settle_seconds)
            points = sorted(set(points))

        snapshots: dict[float, dict[str, dict]] = {}
        last_t = 0.0
        for t in points:
            dt = max(0.0, t - last_t)
            if dt:
                time.sleep(dt)
            cookies = {c["name"]: c for c in (driver.get_cookies() or [])}
            snapshots[t] = {name: _summarize_cookie(cookies.get(name)) for name in _PX_COOKIE_NAMES}
            last_t = t

        px3_initial = snapshots[points[0]]["_px3"]
        px3_final = snapshots[points[-1]]["_px3"]
        pxvid_initial = snapshots[points[0]]["_pxvid"]
        pxvid_final = snapshots[points[-1]]["_pxvid"]
        cookies_final = {c["name"]: c for c in (driver.get_cookies() or [])}

        def timeline(name: str) -> str:
            parts = []
            for t in points:
                present = "present" if snapshots[t][name]["present"] else "absent"
                parts.append(f"{int(t)}s:{present}")
            return " -> ".join(parts)

        out = {
            "perimeterx.page_state": {
                "value": "challenge" if has_px_block else "no visible challenge",
                "status": "failed" if has_px_block else "passed",
            },
            "perimeterx.px3_lifecycle": {
                "value": f"{'present' if px3_initial['present'] else 'absent'} -> {'present' if px3_final['present'] else 'absent'}",
                "status": "failed" if not px3_final["present"] else ("failed" if has_px_block else "passed"),
            },
            "perimeterx.pxvid_lifecycle": {
                "value": f"{'present' if pxvid_initial['present'] else 'absent'} -> {'present' if pxvid_final['present'] else 'absent'}",
                "status": "warn" if not pxvid_final["present"] else ("warn" if has_px_block else "passed"),
            },
            "perimeterx.px_settle_seconds": {
                "value": str(int(settle_seconds) if float(int(settle_seconds)) == settle_seconds else settle_seconds),
                "status": None,
            },
            "perimeterx.px3_timeline": {
                "value": timeline("_px3"),
                "status": "failed" if not px3_final["present"] else ("failed" if has_px_block else "passed"),
            },
            "perimeterx.pxvid_timeline": {
                "value": timeline("_pxvid"),
                "status": "warn" if not pxvid_final["present"] else ("warn" if has_px_block else "passed"),
            },
        }

        if px_error_match:
            out["perimeterx.error_code"] = {"value": px_error_match.group(0).upper(), "status": "failed"}

        px3 = cookies_final.get("_px3")
        pxvid = cookies_final.get("_pxvid")
        if px3 is not None:
            has_value = bool(px3.get("value", ""))
            out["_px3"] = {
                "value": ("present but challenge page" if has_px_block else (px3.get("value") or "")[:80]) or "(empty)",
                "status": "failed" if has_px_block else ("passed" if has_value else "failed"),
            }
            out["_px3.length"] = {
                "value": str(px3_final["length"]),
                "status": "failed" if px3_final["length"] <= 0 else None,
            }
            if px3_final.get("expiry") is not None:
                out["_px3.expiry"] = {"value": str(px3_final.get("expiry")), "status": None}
        else:
            out["_px3"] = {"value": "(absent)", "status": "failed"}
        if pxvid is not None:
            out["_pxvid"] = {
                "value": (pxvid.get("value") or "")[:80] or "(empty)",
                "status": "warn" if has_px_block and pxvid.get("value") else ("passed" if pxvid.get("value") else None),
            }

        # Secondary _px* cookies: emit all that are present
        for name in _PX_COOKIE_NAMES:
            if name in ("_px3", "_pxvid"):
                continue  # already emitted above
            c = cookies_final.get(name)
            if c:
                val = (c.get("value") or "")[:80]
                out[name] = {"value": val or "(empty)", "status": "passed" if val else "warn"}

        # Collect all _px* cookies in a single summary line for easy diff
        all_px_present = sorted(name for name in _PX_COOKIE_NAMES if cookies_final.get(name))
        all_px_absent = sorted(name for name in _PX_COOKIE_NAMES if not cookies_final.get(name))
        out["perimeterx.cookies_present"] = {
            "value": ", ".join(all_px_present) if all_px_present else "(none)",
            "status": "passed" if "_px3" in all_px_present and not has_px_block else ("failed" if not all_px_present else "warn"),
        }
        if all_px_absent:
            out["perimeterx.cookies_absent"] = {"value": ", ".join(all_px_absent), "status": None}

        # PX JS execution + resource telemetry + sensor candidate detection via JavaScript
        try:
            js_raw = driver.execute_script(r"""
            const out = {};
            try {
              // Did PX JS load and execute? Check globals set by px.js and the modern
              // HUMAN Security sensor SDK (micpn.com / first-party proxy deployments use
              // different init patterns: window._pxVid, window._pxUUID, window.PXClient).
              const appId = typeof window._pxAppId !== 'undefined' ? String(window._pxAppId).slice(0, 40) : null;
              const param = typeof window._pxParam !== 'undefined';
              const collector = typeof window._pxCollector !== 'undefined';
              const ac = typeof window._px_a_c !== 'undefined';
              const mobile = typeof window._pxMobile !== 'undefined';
              const vid = typeof window._pxVid !== 'undefined';
              const uuid = typeof window._pxUUID !== 'undefined';
              const client = typeof window.PXClient !== 'undefined';
              const human = typeof window.HumanSecurity !== 'undefined';
              out.px_js_executed = String(appId !== null || param || collector || ac || mobile || vid || uuid || client || human);
              if (appId) out.px_app_id = appId;
              const indicators = [];
              if (param) indicators.push('_pxParam');
              if (collector) indicators.push('_pxCollector');
              if (ac) indicators.push('_px_a_c');
              if (mobile) indicators.push('_pxMobile');
              if (vid) indicators.push('_pxVid');
              if (uuid) indicators.push('_pxUUID');
              if (client) indicators.push('PXClient');
              if (human) indicators.push('HumanSecurity');
              if (indicators.length) out.px_js_globals = indicators.join(', ');
              // HUMAN Security SDK (new) runs as a fully-contained IIFE and does NOT
              // expose the legacy _pxAppId / _pxParam window globals. Fall back to
              // resource-timing evidence: if the sensor script transferred bytes it ran.
              try {
                const SENSOR_RE = /micpn\.com|client\.px-cdn|pxi\.pub|human\.security/i;
                const sensorRan = (performance.getEntriesByType('resource') || [])
                  .some(function(r) { return SENSOR_RE.test(r.name) && r.transferSize > 0 && r.decodedBodySize > 0; });
                if (sensorRan) {
                  out.px_js_executed = 'true';
                  if (!indicators.length) out.px_sensor_found_no_globals = 'true (HUMAN SDK IIFE — no window globals set)';
                }
              } catch(e2) {}
              // Snapshot ALL window properties matching PX / HUMAN naming conventions.
              // Captured pre-interaction so the UI can diff against post-interaction values.
              try {
                const PX_KEY_RE = /^(_px|px_|PX[A-Z_]|Human[A-Z_]|HUMAN[A-Z_])/;
                const pxKeys = Object.getOwnPropertyNames(window)
                  .filter(function(k) { return PX_KEY_RE.test(k); })
                  .sort();
                out.px_globals_count = String(pxKeys.length);
                if (pxKeys.length > 0) {
                  const snap = {};
                  pxKeys.forEach(function(k) {
                    try { snap[k] = typeof window[k]; } catch(e4) { snap[k] = 'error'; }
                  });
                  out.px_globals_snapshot = JSON.stringify(snap).slice(0, 300);
                }
              } catch(e3) {}
            } catch(e) {}
            try {
              // Challenge iframe: PX uses px-cdn.net, cdn.pxi.pub, or first-party subdomains.
              // micpn.com is HUMAN Security's first-party CDN proxy domain.
              const PX_CDN_RE = /px-cdn|pxi\.pub|perimeterx|captcha\.px|micpn\.com/i;
              const frames = Array.from(document.querySelectorAll('iframe'));
              const pxFrame = frames.find(f => PX_CDN_RE.test(f.src || ''));
              if (pxFrame) out.px_challenge_iframe = (pxFrame.src || '').slice(0, 200);
              // reCAPTCHA fallback: only flag when the frame is inside a PX challenge context
              // (a PX iframe is also present, OR the reCAPTCHA parent carries a px/challenge class).
              const rcFrame = frames.find(f => /recaptcha|google\.com\/recaptcha/i.test(f.src || '') || /recaptcha/i.test(f.title || ''));
              if (rcFrame) {
                const par = rcFrame.parentElement;
                const parCtx = ((par ? par.id : '') + ' ' + (par ? par.className : '')).toLowerCase();
                const inPxContext = pxFrame || /px.?captcha|perimeterx|human.?challenge|px.?challenge/i.test(parCtx);
                if (inPxContext) out.px_recaptcha_fallback = 'detected';
              }
            } catch(e) {}
            try {
              // PX resource loading: blocked scripts signal PX was not allowed to run.
              // micpn.com is the HUMAN Security first-party CDN proxy domain used by some
              // enterprise deployments (e.g. Walgreens) to bypass CDN-based blocking.
              const PX_RES_RE = /px-cdn|pxi\.pub|client\.px-cdn|perimeterx\.net|human\.security|micpn\.com/i;
              const entries = performance.getEntriesByType('resource');
              const pxEntries = entries.filter(r => PX_RES_RE.test(r.name));
              out.px_resource_count = String(pxEntries.length);
              const blocked = pxEntries.filter(r => r.transferSize === 0 && r.decodedBodySize === 0 && r.duration < 5);
              out.px_blocked_resource_count = String(blocked.length);
              if (blocked.length > 0) {
                out.px_blocked_sample = blocked.slice(0, 3).map(r => r.name.slice(-80)).join('; ');
              }
              if (pxEntries.length > 0) {
                out.px_resource_sample = pxEntries.slice(0, 3).map(r => r.name.slice(-80)).join('; ');
              }
              // Collector XHR detection: sensor made outbound API calls to the PX/HUMAN
              // data-collection backend. A blocked script can still result in collector
              // calls if the SDK loaded partially — so surface this independently.
              // Collector paths: /api/v*, /xhr/, /telemetry, or explicit 'collector' segment.
              const COLLECTOR_PATH_RE = /\/api\/v\d|\/xhr\/|\/telemetry|\/collector|\bsapi\b/i;
              const collectorCalls = pxEntries.filter(function(r) {
                return COLLECTOR_PATH_RE.test(r.name) && r.initiatorType !== 'script'
                    && r.transferSize > 0;
              });
              out.px_collector_active = String(collectorCalls.length > 0);
              if (collectorCalls.length > 0) {
                out.px_collector_sample = collectorCalls.slice(0, 3).map(function(r) {
                  return r.name.slice(-100) + ' [sz:' + r.transferSize + ' dur:' + Math.round(r.duration) + 'ms]';
                }).join('; ');
              }
              // PX script tags (explicit CDN match including first-party proxy domains)
              const pxScripts = Array.from(document.querySelectorAll(
                'script[src*="px-cdn"], script[src*="pxi.pub"], script[src*="human.security"], script[src*="micpn.com"]'
              ));
              out.px_script_tags = String(pxScripts.length);
            } catch(e) {}
            try {
              // Sensor/bot script candidates: cross-origin scripts or ones matching
              // security/tracking keywords — reveals first-party proxy URLs that bypass
              // the explicit PX CDN pattern above.
              const BOT_RE = /bot|sensor|track|pixel|human|shield|protect|defend|fingerprint|collect|telemetry|behavior|analytics|risk|fraud|challenge|probe|_px|perimeterx/i;
              const origin = location.hostname.replace(/^www\./, '');
              const allSrc = Array.from(document.querySelectorAll('script[src]')).map(s => s.src);
              const candidates = allSrc.filter(u => {
                try {
                  const h = new URL(u).hostname.replace(/^www\./, '');
                  return h !== origin || BOT_RE.test(u);
                } catch(ex) { return false; }
              });
              // Also surface large (>30 KB) external scripts via PerformanceTiming
              // that are not already captured as DOM script tags.
              const resEntries = performance.getEntriesByType('resource');
              const largeSrcs = resEntries
                .filter(r => r.initiatorType === 'script' && r.encodedBodySize > 30000)
                .map(r => r.name)
                .filter(u => {
                  try {
                    const h = new URL(u).hostname.replace(/^www\./, '');
                    return h !== origin;
                  } catch(ex) { return false; }
                });
              const all = Array.from(new Set([...candidates, ...largeSrcs]));
              out.sensor_script_candidate_count = String(all.length);
              if (all.length > 0) {
                out.sensor_script_candidates = all.slice(0, 25).join('\n');
                // Timing per candidate: URL [status sz:transfer/decoded dur:Xms]
                const timings = all.slice(0, 15).map(u => {
                  const es = performance.getEntriesByName(u, 'resource');
                  if (es.length > 0) {
                    const e = es[0];
                    const st = e.transferSize === 0 && e.encodedBodySize === 0 && e.duration < 5 ? 'BLOCKED' : 'ok';
                    return u.slice(-100) + ' [' + st + ' sz:' + e.transferSize + '/' + e.encodedBodySize + ' dur:' + Math.round(e.duration) + 'ms]';
                  }
                  return u.slice(-100) + ' [no-timing]';
                });
                out.sensor_candidate_timings = timings.join('\n');
              }
            } catch(e) {}
            try {
              // reCAPTCHA iframe DOM context: parent/grandparent attributes reveal whether
              // it's embedded inside a PX challenge div or a native site form.
              const rcFrames = Array.from(document.querySelectorAll('iframe[src*="recaptcha"], iframe[title*="recaptcha" i], iframe[src*="hcaptcha"]'));
              const contexts = rcFrames.slice(0, 3).map(f => {
                const par = f.parentElement;
                const gp = par ? par.parentElement : null;
                const parId = par ? ((par.id ? 'id=' + par.id : '') + ' ' + (par.className ? 'class=' + par.className.slice(0, 60) : '')).trim() : '';
                const gpId = gp ? ((gp.id ? 'id=' + gp.id : '') + ' ' + (gp.className ? 'class=' + gp.className.slice(0, 60) : '')).trim() : '';
                return (f.src || '').slice(0, 120) + ' | par[' + parId + '] gp[' + gpId + ']';
              });
              if (contexts.length > 0) out.recaptcha_iframe_context = contexts.join(' || ');
            } catch(e) {}
            try {
              // Cookie surface: count all accessible cookies and list non-PX names.
              const docCookies = document.cookie ? document.cookie.split(';').map(c => c.trim().split('=')[0].trim()).filter(Boolean) : [];
              out.doc_cookie_count = String(docCookies.length);
              const nonPx = docCookies.filter(n => !/^_px|^pxcts/i.test(n)).sort();
              if (nonPx.length > 0) out.non_px_cookie_names = nonPx.slice(0, 20).join(', ');
            } catch(e) {}
            return JSON.stringify(out);
            """) or "{}"
            js_signals = json.loads(js_raw)
            px_executed = str(js_signals.get("px_js_executed", "")).lower()
            if px_executed:
                out["perimeterx.js_executed"] = {
                    "value": px_executed,
                    "status": "passed" if px_executed == "true" else ("failed" if has_px_block else "warn"),
                }
            for key in ("px_app_id", "px_js_globals", "px_resource_count", "px_blocked_resource_count",
                        "px_blocked_sample", "px_resource_sample", "px_script_tags",
                        "px_collector_active", "px_collector_sample",
                        "sensor_script_candidate_count", "sensor_script_candidates",
                        "sensor_candidate_timings", "recaptcha_iframe_context",
                        "doc_cookie_count", "non_px_cookie_names",
                        "px_sensor_found_no_globals", "px_globals_count", "px_globals_snapshot"):
                if key in js_signals:
                    out[f"perimeterx.{key}"] = {"value": str(js_signals[key]), "status": None}
            if "px_challenge_iframe" in js_signals:
                out["perimeterx.challenge_iframe"] = {"value": str(js_signals["px_challenge_iframe"]), "status": "warn"}
            if "px_recaptcha_fallback" in js_signals:
                out["perimeterx.recaptcha_fallback"] = {"value": "detected", "status": "warn"}
            # Blocked PX resources are surfaced via perimeterx.px_blocked_resource_count
            # for observability, but do NOT override js_executed status — the waf_status
            # taxonomy is the authoritative trust indicator; a partial resource load can
            # coexist with trust being granted (observed on Fiverr with 1 blocked asset).
        except Exception:
            pass

        # All cookies via CDP (includes httpOnly + cross-domain cookies set by iframes)
        try:
            cdp_cookies = driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
            out["perimeterx.all_cookie_count"] = {"value": str(len(cdp_cookies)), "status": None}
            by_domain: dict[str, list[str]] = {}
            for c in cdp_cookies:
                d = (c.get("domain") or "").lstrip(".")
                by_domain.setdefault(d, []).append(c.get("name") or "")
            if by_domain:
                # Sort by cookie count descending so the target site domain (most cookies) appears first.
                lines = [f"{d}: {', '.join(sorted(names)[:12])}" for d, names in sorted(by_domain.items(), key=lambda x: -len(x[1]))[:12]]
                out["perimeterx.all_cookies_by_domain"] = {"value": "\n".join(lines), "status": None}
        except Exception:
            pass

        # Interaction-triggered re-check: send a minimal mouse move + scroll, wait 2 s,
        # then re-poll PX globals and cookies. Tests whether the sensor defers trust-cookie
        # issuance until it observes an interaction event — a common PX/HUMAN init pattern.
        try:
            px_executed_now = str((out.get("perimeterx.js_executed") or {}).get("value", "")).lower() == "true"
            px3_now = (out.get("_px3") or {}).get("value", "") not in ("", "(absent)")
            if not px_executed_now or not px3_now:
                try:
                    mouse = HumanMouse(driver)
                    # Phase 1: realistic wander with occasional scrolls
                    mouse.wander(duration=3.0, scroll_probability=0.35, scroll_distance_range=(60, 180))
                    # Phase 2: focus + viewport dwell — move to the page body, trigger
                    # a focus event, then sit idle briefly before a final small scroll.
                    # Many HUMAN sensor trust heuristics check for dwell-after-focus.
                    try:
                        driver.execute_script(
                            "try { document.body && document.body.focus && document.body.focus(); } catch(e){}"
                        )
                        time.sleep(0.4)
                        mouse.wander(duration=1.5, scroll_probability=0.15, scroll_distance_range=(20, 70))
                    except Exception:
                        pass
                except Exception:
                    try:
                        win = driver.execute_script("return {w: window.innerWidth, h: window.innerHeight};") or {}
                        W = int(win.get("w") or 1280)
                        H = int(win.get("h") or 720)
                        cx, cy = W // 2, H // 3
                        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                            "type": "mouseMoved", "x": float(cx), "y": float(cy),
                            "modifiers": 0, "pointerType": "mouse",
                        })
                        driver.execute_cdp_cmd("Input.synthesizeScrollGesture", {
                            "x": cx, "y": cy, "xDistance": 0, "yDistance": -120, "speed": 300,
                        })
                    except Exception:
                        pass
                time.sleep(2.0)
                # Re-check globals
                post_js_raw = driver.execute_script(r"""
                const g = {};
                try {
                  const vid = typeof window._pxVid !== 'undefined';
                  const uuid = typeof window._pxUUID !== 'undefined';
                  const client = typeof window.PXClient !== 'undefined';
                  const human = typeof window.HumanSecurity !== 'undefined';
                  const ac = typeof window._px_a_c !== 'undefined';
                  const param = typeof window._pxParam !== 'undefined';
                  const appId = typeof window._pxAppId !== 'undefined' ? String(window._pxAppId).slice(0,40) : null;
                  g.executed = String(appId !== null || param || ac || vid || uuid || client || human);
                  const ind = [];
                  if (vid) ind.push('_pxVid'); if (uuid) ind.push('_pxUUID');
                  if (client) ind.push('PXClient'); if (human) ind.push('HumanSecurity');
                  if (ac) ind.push('_px_a_c'); if (param) ind.push('_pxParam');
                  if (ind.length) g.globals = ind.join(', ');
                  // Same HUMAN SDK IIFE fallback as the pre-interaction check
                  try {
                    const SENSOR_RE = /micpn\.com|client\.px-cdn|pxi\.pub|human\.security/i;
                    const sensorRan = (performance.getEntriesByType('resource') || [])
                      .some(function(r) { return SENSOR_RE.test(r.name) && r.transferSize > 0 && r.decodedBodySize > 0; });
                    if (sensorRan) g.executed = 'true';
                  } catch(e2) {}
                  // Post-interaction globals snapshot for diff vs pre-interaction
                  try {
                    const PX_KEY_RE = /^(_px|px_|PX[A-Z_]|Human[A-Z_]|HUMAN[A-Z_])/;
                    const pxKeys = Object.getOwnPropertyNames(window)
                      .filter(function(k) { return PX_KEY_RE.test(k); })
                      .sort();
                    g.px_globals_count_post = String(pxKeys.length);
                    if (pxKeys.length > 0) {
                      const snap = {};
                      pxKeys.forEach(function(k) {
                        try { snap[k] = typeof window[k]; } catch(e4) { snap[k] = 'error'; }
                      });
                      g.px_globals_snapshot_post = JSON.stringify(snap).slice(0, 500);
                    }
                  } catch(e3) {}
                } catch(e) {}
                return JSON.stringify(g);
                """) or "{}"
                post_js = json.loads(post_js_raw)
                post_executed = str(post_js.get("executed", "")).lower() == "true"
                out["perimeterx.post_interaction_js_executed"] = {
                    "value": str(post_executed).lower(),
                    "status": "passed" if post_executed else ("warn" if not px_executed_now else None),
                }
                if post_js.get("globals"):
                    out["perimeterx.post_interaction_globals"] = {"value": post_js["globals"], "status": None}
                if post_js.get("px_globals_count_post") is not None:
                    out["perimeterx.px_globals_count_post"] = {"value": post_js["px_globals_count_post"], "status": None}
                if post_js.get("px_globals_snapshot_post"):
                    out["perimeterx.px_globals_snapshot_post"] = {"value": post_js["px_globals_snapshot_post"], "status": None}
                # Re-check cookies
                post_cookies = {c["name"]: c for c in (driver.get_cookies() or [])}
                post_px3 = post_cookies.get("_px3")
                post_pxvid = post_cookies.get("_pxvid")
                all_post_px = sorted(n for n in _PX_COOKIE_NAMES if post_cookies.get(n))
                out["perimeterx.post_interaction_cookies"] = {
                    "value": ", ".join(all_post_px) if all_post_px else "(none)",
                    "status": "passed" if "_px3" in all_post_px else ("warn" if all_post_px else None),
                }
                if post_px3:
                    val = (post_px3.get("value") or "")[:80]
                    out["perimeterx.post_interaction_px3"] = {
                        "value": val or "(empty)",
                        "status": "passed" if val else "failed",
                    }
                # Signal whether the interaction unblocked the sensor
                newly_executed = post_executed and not px_executed_now
                newly_trusted = bool(post_px3) and not px3_now
                if newly_executed or newly_trusted:
                    out["perimeterx.interaction_unblocked_sensor"] = {
                        "value": f"yes (executed={newly_executed}, _px3_appeared={newly_trusted})",
                        "status": "warn",
                    }
        except Exception:
            pass

        # Browser console errors (requires goog:loggingPrefs to be set on ChromeOptions)
        try:
            browser_logs = driver.get_log("browser") or []
            severe = [l for l in browser_logs if l.get("level") in ("SEVERE", "WARNING")]
            if severe:
                out["perimeterx.console_error_count"] = {"value": str(len(severe)), "status": None}
                formatted = [f"[{l.get('level','?')}] {str(l.get('message', ''))[:300]}" for l in severe[:10]]
                out["perimeterx.console_errors"] = {"value": "\n".join(formatted), "status": None}
        except Exception:
            pass

        # WAF status taxonomy — five states ordered by diagnostic specificity.
        # hard_block             : PXCR* error code present (explicit PX block response)
        # visible_challenge      : press-and-hold / interactive challenge rendered
        # collector_active_no_trust: sensor ran AND made collector XHR calls, but _px3
        #   not issued — server evaluated the telemetry and withheld trust
        # sensor_active_no_trust : sensor ran but no collector calls visible — sensor may
        #   be operating in a reduced/deferred mode (e.g. Walgreens micpn.com path)
        # sensor_absent          : no evidence the sensor script executed at all
        # clean                  : sensor ran AND _px3 issued AND no visible challenge
        try:
            _px_error = bool((out.get("perimeterx.error_code") or {}).get("value"))
            _vis_challenge = has_px_block
            _js_ran = str((out.get("perimeterx.js_executed") or {}).get("value", "")).lower() == "true"
            _px3_ok = (out.get("_px3") or {}).get("value", "(absent)") not in (
                "(absent)", "", "present but challenge page"
            )
            _collector_ran = str(
                (out.get("perimeterx.px_collector_active") or {}).get("value", "")
            ).lower() == "true"
            if _px_error:
                _waf_status, _waf_st = "hard_block", "failed"
            elif _vis_challenge:
                _waf_status, _waf_st = "visible_challenge", "failed"
            elif _js_ran and not _px3_ok and _collector_ran:
                _waf_status, _waf_st = "collector_active_no_trust", None
            elif _js_ran and not _px3_ok:
                _waf_status, _waf_st = "sensor_active_no_trust", None
            elif not _js_ran:
                _waf_status, _waf_st = "sensor_absent", "warn"
            else:
                _waf_status, _waf_st = "clean", "passed"
            out["perimeterx.waf_status"] = {"value": _waf_status, "status": _waf_st}
        except Exception:
            pass

        return out if out else None
    except Exception:
        return None


def _extract_turnstile(driver) -> dict | None:
    """
    Check whether a Cloudflare Turnstile challenge resolved without looping.

    Polls up to ~8 s since Turnstile typically resolves within 3 s in clean
    contexts.  Distinguishes three outcomes:
    - passed        : cf-turnstile-response token generated
    - failed        : multiple challenge iframes = infinite-loop state
    - pending_timeout : token still absent after the polling window
    """
    script = r"""
    const tokenEl = document.querySelector('[name="cf-turnstile-response"]');
    const tokenValue = tokenEl ? tokenEl.value : null;
    const challengeFrames = document.querySelectorAll('iframe[src*="challenges.cloudflare.com"]').length;
    return JSON.stringify({
        token_present: !!(tokenValue && tokenValue.length > 0),
        token_value: tokenValue ? tokenValue.slice(0, 40) + '...' : null,
        challenge_frame_count: challengeFrames
    });
    """
    try:
        data: dict = {}
        for _ in range(8):
            raw = driver.execute_script(script)
            if raw:
                data = json.loads(raw)
                if data.get("token_present"):
                    break
            time.sleep(1.0)

        token_present = bool(data.get("token_present"))
        frame_count = int(data.get("challenge_frame_count") or 0)

        if token_present:
            return {"cf_turnstile_response": {"value": data.get("token_value") or "(token present)", "status": "passed"}}
        if frame_count > 1:
            return {"cf_turnstile_response": {"value": "(looping)", "status": "failed"}}
        # Token absent and no loop: not a challenge failure, just timed out.
        return {"cf_turnstile_response": {"value": "(pending_timeout)", "status": "warn"}}
    except Exception:
        return None


def _extract_cloudflare_quic(driver) -> dict | None:
    """
    Capture negotiated protocol hints from cloudflare-quic.com.

    This is a measurement-only check: we simply report what the browser thinks
    it negotiated (e.g. "h3" vs "h2") and what the page text indicates.
    """
    script = r"""
    const nav = (performance && performance.getEntriesByType)
      ? (performance.getEntriesByType('navigation') || [])[0]
      : null;
    const nextHop = nav ? (nav.nextHopProtocol || '') : '';
    const bodyText = document.body ? (document.body.innerText || '') : '';
    // Heuristic: cloudflare-quic page prints "HTTP/3" + "QUIC" when enabled.
    // Avoid regex literals here (they have proven brittle across WebDriver transports).
    const norm = String(bodyText || '').toLowerCase();
    const hasH3Text =
      norm.includes('http/3') ||
      norm.includes('http 3') ||
      norm.includes('quic') ||
      norm.includes('h3');
    return JSON.stringify({ nextHopProtocol: nextHop, hasH3Text });
    """
    try:
        raw = driver.execute_script(script)
        if not raw:
            return None
        data = json.loads(raw)
        proto = str(data.get("nextHopProtocol") or "").strip()
        has_h3 = bool(re.search(r"\bh3\b|http\/3", proto, flags=re.IGNORECASE)) or bool(data.get("hasH3Text"))
        return {
            "next_hop_protocol": _signal(proto or "(unknown)", "passed" if has_h3 else "warn"),
        }
    except Exception:
        return None


def _extract_cloudflare_trace(driver) -> dict | None:
    """
    Parse Cloudflare's plain-text trace endpoint.

    This is useful for egress sanity checks because it exposes the Cloudflare
    colo, IP, TLS, HTTP protocol, and coarse location hints without relying on a
    heavyweight JS challenge page.
    """
    try:
        text = driver.execute_script("return document.body ? (document.body.innerText || '') : '';") or ""
    except Exception:
        return None

    trace: dict[str, str] = {}
    for raw_line in str(text).splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key:
            trace[key] = value

    if not trace:
        return None

    out: dict = {}
    for key, label in (
        ("ip",   "cloudflare_trace.ip"),
        ("colo", "cloudflare_trace.colo"),
        ("loc",  "cloudflare_trace.loc"),
        ("http", "cloudflare_trace.http"),
        ("tls",  "cloudflare_trace.tls"),
        ("uag",  "cloudflare_trace.user_agent"),
    ):
        if key in trace:
            out[label] = _signal(trace.get(key), None)

    # Capture browser locale signals in the same page context so that ISP /
    # proxy timezone drift (trace loc vs Intl timezone) can be compared.
    try:
        locale_raw = driver.execute_script(
            r"""
            return JSON.stringify({
                timezone:  Intl.DateTimeFormat().resolvedOptions().timeZone || '',
                language:  navigator.language  || '',
                languages: Array.from(navigator.languages || []).join(', '),
            });
            """
        )
        if locale_raw:
            ld = json.loads(locale_raw)
            if ld.get("timezone"):
                out["browser.timezone"] = _signal(ld["timezone"], None)
            if ld.get("language"):
                out["browser.language"] = _signal(ld["language"], None)
            if ld.get("languages"):
                out["browser.languages"] = _signal(ld["languages"], None)
    except Exception:
        pass

    return out or None


def _extract_recaptcha_demo(driver) -> dict | None:
    """
    Detect the presence of a reCAPTCHA widget on the Google demo page.

    We intentionally do *not* attempt to solve any CAPTCHAs automatically.
    """
    script = r"""
    const widget = document.querySelector('.g-recaptcha, [data-sitekey][data-callback], iframe[src*="recaptcha"]');
    const iframeCount = document.querySelectorAll('iframe[src*="recaptcha"]').length;
    const widgetCount = document.querySelectorAll('.g-recaptcha, [data-sitekey]').length;
    return JSON.stringify({ widget: Boolean(widget), iframeCount, widgetCount });
    """
    try:
        raw = driver.execute_script(script)
        if not raw:
            return None
        data = json.loads(raw)
        widget_present = bool(data.get("widget"))
        iframe_count = int(data.get("iframeCount") or 0)
        return {
            "recaptcha_widget_present": _signal(str(widget_present).lower(), "warn" if widget_present else "failed"),
            "recaptcha_iframe_count": _signal(str(iframe_count), "warn" if widget_present else None),
        }
    except Exception:
        return None


def _extract_hcaptcha_demo(driver) -> dict | None:
    """
    Detect the presence of an hCaptcha widget on the demo page.

    We intentionally do *not* attempt to solve any CAPTCHAs automatically.
    """
    script = r"""
    const widget = document.querySelector('.h-captcha, iframe[src*="hcaptcha"], textarea[name="h-captcha-response"]');
    const iframeCount = document.querySelectorAll('iframe[src*="hcaptcha"]').length;
    const widgetCount = document.querySelectorAll('.h-captcha').length;
    const tokenEl = document.querySelector('textarea[name="h-captcha-response"]');
    const tokenValue = tokenEl ? (tokenEl.value || '') : '';
    return JSON.stringify({ widget: Boolean(widget), iframeCount, widgetCount, tokenPresent: Boolean(tokenValue) });
    """
    try:
        raw = driver.execute_script(script)
        if not raw:
            return None
        data = json.loads(raw)
        widget_present = bool(data.get("widget"))
        iframe_count = int(data.get("iframeCount") or 0)
        token_present = bool(data.get("tokenPresent"))
        status = "passed" if token_present else ("warn" if widget_present else "failed")
        return {
            "hcaptcha_widget_present": _signal(str(widget_present).lower(), "warn" if widget_present else "failed"),
            "hcaptcha_iframe_count": _signal(str(iframe_count), "warn" if widget_present else None),
            # This is diagnostic-only: token appears only after manual solve.
            "hcaptcha_token_present": _signal(str(token_present).lower(), status),
        }
    except Exception:
        return None


def _extract_scamalytics(driver) -> dict | None:
    """
    Parse https://scamalytics.com/ip/{ip} for IP fraud score and risk indicators.

    The URL is rewritten by run_probe to include the egress IP learned from
    ipinfo.io (which appears earlier in the probe order), so this extractor
    always operates on the IP-specific result page rather than the search form.
    """
    script = r"""
    const out = {};
    const set = (k, v, st=null) => { out[k] = { value: String(v ?? '').trim().slice(0, 220), status: st }; };
    const body = document.body ? (document.body.innerText || document.body.textContent || '') : '';

    // Fraud score (0–100): look for "Fraud Score: N" pattern
    const scoreMatch = body.match(/fraud\s*score\s*:?\s*(\d{1,3})/i);
    if (scoreMatch) {
        const s = parseInt(scoreMatch[1], 10);
        // 0-25 = low risk, 26-75 = medium, 76-100 = high
        const st = s <= 25 ? 'passed' : s <= 75 ? 'warn' : 'failed';
        set('scamalytics.fraud_score', String(s), st);
    }

    // Verdict string: "Non-Fraudulent", "High Risk", "Low Risk", etc.
    const verdictMatch = body.match(/(non[\s-]fraudulent|very\s+high\s+risk|high\s+risk|medium\s+risk|low\s+risk|suspicious)[^<\n]{0,80}/i);
    if (verdictMatch) {
        const v = verdictMatch[0].trim().slice(0, 80);
        const lower = v.toLowerCase();
        const st = /very\s+high|^high/.test(lower) ? 'failed' : /medium|suspicious/.test(lower) ? 'warn' : 'passed';
        set('scamalytics.verdict', v, st);
    }

    // IP confirmed on the page (sanity check that URL rewriting worked)
    const ipv4Match = body.match(/\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b/);
    if (ipv4Match) set('scamalytics.checked_ip', ipv4Match[1], null);

    // ISP and organization
    // The page renders "ISP  Name\tCharter Communications" where "Name" is a sub-label column.
    // Strip that sub-label prefix before storing.
    const ispMatch = body.match(/\bISP\s*:?\s*([^\n]+)/i);
    if (ispMatch) {
        const isp = ispMatch[1].trim().replace(/^Name\s+/i, '').trim().slice(0, 100);
        set('scamalytics.isp', isp, null);
    }
    const orgMatch = body.match(/\bOrganization\s*:?\s*([^\n]+)/i);
    if (orgMatch) set('scamalytics.organization', orgMatch[1].trim().replace(/^Name\s+/i, '').trim().slice(0, 100), null);
    const countryMatch = body.match(/\bCountry\s*:?\s*([^\n]+)/i);
    if (countryMatch) set('scamalytics.country', countryMatch[1].trim().slice(0, 60), null);

    // Risk flags: VPN / Proxy / TOR / Crawler
    // Allow the Yes/No value to appear on the same line (tab-separated) or the next line.
    for (const [label, key] of [['VPN', 'vpn'], ['Proxy', 'proxy'], ['TOR', 'tor'], ['Crawler', 'crawler']]) {
        const re = new RegExp('\\b' + label + '\\b[^\\n]*\\n?[^\\n]{0,20}?(Yes|No)', 'i');
        const m = body.match(re);
        if (m) {
            const isYes = m[1].toLowerCase() === 'yes';
            set('scamalytics.' + key, isYes ? 'yes' : 'no', isYes ? 'failed' : 'passed');
        }
    }

    return Object.keys(out).length ? JSON.stringify(out) : null;
    """
    try:
        raw = driver.execute_script(script)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _extract_ipqualityscore(driver) -> dict | None:
    """
    Best-effort parsing for the IPQualityScore lookup page.

    The page is dynamic and may change; if extraction fails we still rely on the
    saved HTML + screenshot diagnostics for manual inspection.
    """
    try:
        text = driver.execute_script("return document.body ? (document.body.innerText || '') : '';") or ""
    except Exception:
        return None

    if not text:
        return None

    out: dict = {}

    ip_match = re.search(r"([0-9a-fA-F:.]{7,})\s*\(This is your IP\)", text)
    if ip_match:
        out["ipqs.ip"] = _signal(ip_match.group(1), None)

    # Very loose parsing: look for rows containing "VPN" / "PROXY" / "TOR" and adjacent "Yes|No".
    def _yes_no(label: str) -> str | None:
        match = re.search(rf"\b{re.escape(label)}\b\s*(Yes|No)\b", text, flags=re.IGNORECASE)
        if not match:
            return None
        return match.group(1).strip().lower()

    for label in ("VPN", "PROXY", "TOR"):
        value = _yes_no(label)
        if value is None:
            continue
        status = "passed" if value == "no" else "failed"
        out[f"ipqs.{label.lower()}"] = _signal(value, status)

    # Risk summary: capture a short excerpt near the "Risk Summary" header.
    risk_match = re.search(r"Risk Summary\s*\n([^\n]+)", text, flags=re.IGNORECASE)
    if risk_match:
        out["ipqs.risk_summary"] = _signal(risk_match.group(1).strip(), None)

    # A numeric risk score sometimes appears standalone (0-100). Only surface if obviously labeled.
    score_match = re.search(r"\bRisk\b[^\n]{0,20}\bScore\b[^\d]{0,10}(\d{1,3})\b", text, flags=re.IGNORECASE)
    if score_match:
        out["ipqs.risk_score"] = _signal(score_match.group(1), None)

    # Rewrite the risk_summary with a status so the UI can color it meaningfully.
    summary_signal = out.get("ipqs.risk_summary")
    if isinstance(summary_signal, dict) and summary_signal.get("value"):
        lower = summary_signal["value"].lower()
        if any(w in lower for w in ("fraud", "high risk", "critical", "very high")):
            summary_signal["status"] = "failed"
        elif any(w in lower for w in ("suspicious", "medium", "moderate", "caution", "elevated")):
            summary_signal["status"] = "warn"
        else:
            summary_signal["status"] = "passed"

    return out or None


def _extract_amiunique(driver) -> dict | None:
    """
    Best-effort parsing for amiunique.org/fingerprint.

    The page can be very large. We summarize the headline uniqueness message and
    extract:
      - Topline OS/browser/language/timezone (+ share % when present)
      - HTTP header attributes table (+ share % when present)
      - Javascript attributes table (+ share % when present)
    plus compact section counts for quick render-completeness checks.
    """
    script = r"""
    const trunc = (value, limit = 220) => {
      const text = String(value ?? '').replace(/\s+/g, ' ').trim();
      return text.length <= limit ? text : text.slice(0, limit - 3) + '...';
    };
    const out = {};
    const set = (k, value, status = null) => { out[k] = { value: trunc(value), status }; };
    const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
    const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
    const slug = (value) => String(value || '')
      .trim()
      .toLowerCase()
      .replace(/[%()\\[\\]{}]/g, '')
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '');

    try {
      if (document.title) set('page_title', document.title, null);
      const h1 = document.querySelector('h1');
      if (h1) set('h1', textOf(h1), null);
    } catch (e) {}

    const bodyText = document.body ? (document.body.innerText || document.body.textContent || '') : '';
    if (bodyText) {
      const uniq = bodyText.match(/unique\s+among\s+the\s+([0-9]+)\s+fingerprints/i);
      if (uniq) set('amiunique.population', uniq[1], null);
      // Uniqueness message is sometimes split across line breaks mid-word (responsive layout).
      const idx = bodyText.indexOf('Yes! You are unique');
      if (idx >= 0) {
        const tail = bodyText.slice(idx, idx + 240);
        const parts = tail.split('\n').map(s => norm(s)).filter(Boolean);
        const msg = parts.slice(0, 3).join(' ').replace(/\s+/g, ' ').trim();
        if (msg) set('amiunique.unique_message', msg, 'passed');
      } else {
        const alt = bodyText.match(/Almost!\s*You are[^\\n]{0,140}/i);
        if (alt) set('amiunique.unique_message', alt[0], 'warn');
      }
    }

    // Topline cards (OS / Browser / Language / Timezone)
    try {
      const uniqHeader = Array.from(document.querySelectorAll('h1,h2,h3,h4')).find(h => /are you unique/i.test(norm(textOf(h)).toLowerCase()));
      const uniqCard = uniqHeader ? (uniqHeader.closest('.v-card') || uniqHeader.parentElement) : null;
      if (uniqCard) {
        const cards = Array.from(uniqCard.querySelectorAll('.row .text-center.v-card'));
        for (const card of cards) {
          const label = norm(textOf(card.querySelector('.v-card__subtitle'))).toLowerCase();
          const value = norm(textOf(card.querySelector('.v-card__title')));
          const progress = card.querySelector('.v-progress-linear[aria-valuenow]');
          const pct = progress ? String(progress.getAttribute('aria-valuenow') || '').trim() : '';
          const pctText = pct ? `${pct}%` : '';
          if (!label) continue;
          if (label === 'operating system') {
            set('amiunique.os', value, value ? 'passed' : 'warn');
            if (pctText) set('amiunique.os._share_pct', pctText, null);
          } else if (label === 'web browser') {
            set('amiunique.browser', value, value ? 'passed' : 'warn');
            if (pctText) set('amiunique.browser._share_pct', pctText, null);
          } else if (label === 'language') {
            set('amiunique.language', value, value ? 'passed' : 'warn');
            if (pctText) set('amiunique.language._share_pct', pctText, null);
          } else if (label === 'timezone') {
            set('amiunique.timezone', value, value ? 'passed' : 'warn');
            if (pctText) set('amiunique.timezone._share_pct', pctText, null);
          }
        }
      }
    } catch (e) {}

    const extractTableRows = (sectionTitle, sectionName) => {
      try {
        const wanted = norm(sectionTitle).toLowerCase();
        const header = Array.from(document.querySelectorAll('h1,h2,h3,h4')).find(h => norm(textOf(h)).toLowerCase() === wanted);
        if (!header) return [];
        // Vuetify structure is:
        //   <div class="v-card__title"> <h3>Section</h3> </div>
        //   <div class="v-data-table"> <table>...</table> </div>
        const titleContainer = header.closest('.v-card__title') || header.parentElement;
        let dataTable = titleContainer ? titleContainer.nextElementSibling : null;
        let table = dataTable ? dataTable.querySelector('table') : null;
        if (!table) {
          const card = header.closest('.v-card') || (titleContainer ? titleContainer.parentElement : null);
          if (card) {
            const candidates = Array.from(card.querySelectorAll('.v-data-table'));
            const after = titleContainer || header;
            const next = candidates.find(el => {
              try { return Boolean(after.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING); } catch (e) { return false; }
            });
            if (next) {
              dataTable = next;
              table = next.querySelector('table');
            }
          }
        }
        if (!table) return [];
        const rows = [];
        const trs = Array.from(table.querySelectorAll('tbody tr'));
        for (const tr of trs) {
          const tds = tr.querySelectorAll('td');
          if (!tds || tds.length < 3) continue;
          const labelText = textOf(tds[0]);
          const parts = String(labelText || '').split(/\n/).map(s => String(s || '').trim()).filter(Boolean);
          let firstLine = parts[0] || '';
          // Vuetify tables sometimes render the row number and label on separate lines:
          //   "1 -" then "User agent"
          if (/^\d+\s*-\s*$/.test(firstLine) && parts.length >= 2) {
            firstLine = `${firstLine} ${parts[1]}`.trim();
          }
          const m = firstLine.match(/^\s*(\d+)\s*-\s*(.+)\s*$/);
          const label = (m ? m[2] : firstLine).trim();
          if (!label) continue;

          let pct = textOf(tds[1]) || '';
          pct = String(pct || '').replace(/\s+/g, ' ').trim();
          if (pct && !pct.includes('%') && /^\d+(?:\.\d+)?$/.test(pct)) pct = pct + ' %';

          const value = textOf(tds[2]) || '';
          rows.push({ section: sectionName, label, pct, value });
        }
        return rows;
      } catch (e) {
        return [];
      }
    };

    const rows = [
      ...extractTableRows('HTTP headers attributes', 'http headers attributes'),
      ...extractTableRows('Javascript attributes', 'javascript attributes'),
    ];

    const sectionCounts = {};
    for (const row of rows) {
      const s = String(row.section || 'unknown');
      sectionCounts[s] = (sectionCounts[s] || 0) + 1;
    }

    const map = [
      ['User agent', 'amiunique.user_agent'],
      ['WebGL Vendor', 'amiunique.webgl_vendor'],
      ['WebGL Renderer', 'amiunique.webgl_renderer'],
    ];

    for (const [label, key] of map) {
      const wanted = String(label || '').toLowerCase();
      const hit = rows.find(r => String(r.label || '').toLowerCase().startsWith(wanted)) || null;
      if (!hit) continue;
      set(key, hit.value, hit.value ? 'passed' : 'warn');
      if (hit.pct && hit.pct.includes('%')) set(key + '._share_pct', hit.pct.replace(/\s+/g, ''), null);
    }

    // Nuxt server-provided request headers (fills gaps like If-None-Match even when not shown in the visible table)
    try {
      const state = (window.__NUXT__ && window.__NUXT__.state) ? window.__NUXT__.state : null;
      const headers = state && state.headers ? state.headers : null;
      if (headers) {
        const pick = (k) => (headers[k] || headers[k.toLowerCase()] || headers[k.replace(/-/g,'_')] || headers[k.replace(/_/g,'-')] || '');
        const ua = pick('user-agent');
        const accept = pick('accept');
        const enc = pick('accept-encoding');
        const lang = pick('accept-language');
        const ifNoneMatch = pick('if-none-match');
        const upgrade = pick('upgrade-insecure-requests');
        if (ua) set('amiunique.http.user_agent', ua, null);
        if (accept) set('amiunique.http.accept', accept, null);
        if (enc) set('amiunique.http.content_encoding', enc, null);
        if (lang) set('amiunique.http.content_language', lang, null);
        if (ifNoneMatch) set('amiunique.http.if_none_match', ifNoneMatch, null);
        if (upgrade) set('amiunique.http.upgrade_insecure_requests', upgrade, null);
      }
    } catch (e) {}

    // Emit per-row structured signals for high coverage.
    // Keys are normalized so the UI can search/filter consistently.
    const sectionPrefix = (section) => {
      const s = String(section || '').toLowerCase();
      if (s === 'http headers attributes') return 'amiunique.http.';
      if (s === 'javascript attributes') return 'amiunique.js.';
      if (!s || s === 'unknown') return 'amiunique.unknown.';
      return `amiunique.${slug(s)}.`;
    };

    // Counts per section (useful for quickly spotting partial renders)
    for (const [section, count] of Object.entries(sectionCounts)) {
      const key = `amiunique.section.${slug(section)}.row_count`;
      set(key, String(count), count >= 1 ? 'passed' : 'warn');
    }
    const httpCount = sectionCounts['http headers attributes'] || 0;
    // Visible table often shows 5; If-None-Match can be available via Nuxt headers (see amiunique.http.if_none_match).
    set('amiunique.expected_http_header_row_count', '5', null);
    set('amiunique.http_header_row_count', String(httpCount), httpCount >= 5 ? 'passed' : 'warn');
    const jsCount = sectionCounts['javascript attributes'] || 0;
    set('amiunique.expected_js_attr_row_count', '57', null);
    set('amiunique.js_attr_row_count', String(jsCount), jsCount >= 57 ? 'passed' : 'warn');

    // Store rows (bounded, but should cover the whole report in practice)
    // Even if a value is large (fonts/plugins), trunc() keeps it compact.
    for (const row of rows.slice(0, 260)) {
      const base = sectionPrefix(row.section) + slug(row.label || 'attribute');
      if (!base || base.endsWith('.')) continue;
      set(base, row.value, null);
      if (row.pct && String(row.pct).includes('%')) set(base + '._share_pct', String(row.pct).replace(/\s+/g, ''), null);
    }
    if (rows.length > 260) {
      set('amiunique.rows_truncated', `true (${rows.length} rows)`, 'warn');
    }

    return Object.keys(out).length ? JSON.stringify(out) : null;
    """
    try:
        raw = driver.execute_script(script)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _wait_for_amiunique_render(driver, timeout_seconds: float = 45.0, *, headless: bool = False) -> dict | None:
    """
    amiunique.org/fingerprint is a large Vue/Nuxt app. The probe's base + extra sleeps can
    capture mid-load DOM (v-card--loading, v-data-table__progress), which leads to missing
    topline values and partial attribute tables.

    This helper polls for the main fingerprint card and attribute tables to finish loading.
    """
    start = time.time()
    last = None
    loaded_at = None
    nudge_env = os.environ.get("RE_ANALYZER_SCROLL_NUDGE", "").strip().lower()
    if nudge_env in {"1", "true", "yes"}:
        nudge_scroll = True
    elif nudge_env in {"0", "false", "no"}:
        nudge_scroll = False
    else:
        nudge_scroll = bool(headless)
    while (time.time() - start) < timeout_seconds and not _stop_flag.is_set():
        try:
            last = driver.execute_script(
                r"""
                const textOf = (el) => (el ? (el.innerText || el.textContent || '').trim() : '');
                const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
                const getRows = (title) => {
                  const wanted = norm(title).toLowerCase();
                  const header = Array.from(document.querySelectorAll('h1,h2,h3,h4')).find(h => norm(textOf(h)).toLowerCase() === wanted);
                  if (!header) return 0;
                  const titleContainer = header.closest('.v-card__title') || header.parentElement;
                  let dataTable = titleContainer ? titleContainer.nextElementSibling : null;
                  let table = dataTable ? dataTable.querySelector('table') : null;
                  if (!table) {
                    const card = header.closest('.v-card') || (titleContainer ? titleContainer.parentElement : null);
                    if (card) {
                      const candidates = Array.from(card.querySelectorAll('.v-data-table'));
                      const after = titleContainer || header;
                      const next = candidates.find(el => {
                        try { return Boolean(after.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING); } catch (e) { return false; }
                      });
                      if (next) {
                        dataTable = next;
                        table = next.querySelector('table');
                      }
                    }
                  }
                  if (!table) return 0;
                  return (table.querySelectorAll('tbody tr') || []).length;
                };
                const bodyText = document.body ? (document.body.innerText || '') : '';
                const pctCells = Array.from(document.querySelectorAll('table tbody tr td:nth-child(2)'))
                  .map(textOf)
                  .filter(Boolean);
                return {
                  loadingCards: document.querySelectorAll('.v-card.v-card--loading').length,
                  progressRows: document.querySelectorAll('tr.v-data-table__progress').length,
                  httpRows: getRows('HTTP headers attributes'),
                  jsRows: getRows('Javascript attributes'),
                  hasUniqueLine: /(?:Yes!|Almost!)[^\\n]*unique/i.test(bodyText),
                  hasToplineLabels: ['Operating system', 'Web browser', 'Language', 'Timezone'].every(k => bodyText.includes(k)),
                  hasPct: pctCells.some(v => v.includes('%')),
                  samplePct: pctCells.slice(0, 3),
                };
                """
            )
        except Exception:
            break

        if isinstance(last, dict):
            loaded = (last.get("loadingCards", 1) == 0) and (last.get("progressRows", 1) == 0)
            if loaded and loaded_at is None:
                loaded_at = time.time()

            has_sections = (last.get("httpRows", 0) >= 1) and (last.get("jsRows", 0) >= 1)
            has_summary = bool(last.get("hasUniqueLine") or last.get("hasToplineLabels"))

            # Preferred: wait for the expected row counts (when available).
            if loaded and has_sections and has_summary and last.get("httpRows", 0) >= 5 and last.get("jsRows", 0) >= 57:
                return last

            # Fallback: if the app reports "loaded" but the counts never reach expected,
            # don't block the probe run indefinitely.
            if loaded and loaded_at is not None and (time.time() - loaded_at) > 12 and has_sections:
                return last

        # Nudge lazy rendering / observers only when explicitly enabled.
        if nudge_scroll:
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
        time.sleep(0.5)

    return last


def _wait_for_creepjs_render(driver, timeout_seconds: float = 60.0, *, headless: bool = False) -> dict | None:
    """
    creepjs.org renders asynchronously and can stay on an "Analyzing your browser..." view
    for a while. Poll for key UI text (lies/trust) so extraction happens after results render.
    """
    start = time.time()
    last = None
    loaded_at = None
    nudge_env = os.environ.get("RE_ANALYZER_SCROLL_NUDGE", "").strip().lower()
    if nudge_env in {"1", "true", "yes"}:
        nudge_scroll = True
    elif nudge_env in {"0", "false", "no"}:
        nudge_scroll = False
    else:
        nudge_scroll = bool(headless)
    while (time.time() - start) < timeout_seconds and not _stop_flag.is_set():
        try:
            last = driver.execute_script(
                r"""
                const text = (document.body && (document.body.innerText || document.body.textContent)) ? (document.body.innerText || document.body.textContent) : '';
                const normalize = (s) => String(s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                const h2s = Array.from(document.querySelectorAll('h2')).map(h => normalize(h.innerText || h.textContent || '')).filter(Boolean);
                const analyzing = h2s.some(t => t.includes('analyzing your browser'));
                const hasH1 = Array.from(document.querySelectorAll('h1')).some(h => normalize(h.innerText || h.textContent || '').includes('creepjs'));

                const challengeLike = Boolean(
                  document.querySelector('#cf-challenge-running, #challenge-form, iframe[src*=\"challenges.cloudflare.com\"], iframe[src*=\"hcaptcha.com\"], iframe[src*=\"recaptcha/api2\"], iframe[src*=\"google.com/recaptcha\"]') ||
                  /checking your browser|attention required|ddos protection|verify you are human/i.test(text)
                );

                const hasLies = /(\d+)\s*(?:lie|lies)\b/i.test(text) || /\blies\b/i.test(text);
                const hasTrust = /trust\b[^%]{0,60}(\d{1,3}(?:\.\d+)?)\s*%/i.test(text) || /(\d{1,3}(?:\.\d+)?)\s*%[^\\n]{0,60}trust/i.test(text);

                return {
                  readyState: document.readyState || '',
                  hasH1,
                  analyzing,
                  challengeLike,
                  hasLies,
                  hasTrust,
                };
                """
            )
        except Exception:
            break

        if isinstance(last, dict):
            ready = str(last.get("readyState") or "").lower() == "complete"
            if ready and loaded_at is None:
                loaded_at = time.time()

            if last.get("challengeLike"):
                return last

            if last.get("analyzing"):
                # Prefer waiting until the analyzing banner clears.
                pass
            else:
                # Prefer waiting until both metrics show up.
                if last.get("hasLies") and last.get("hasTrust"):
                    return last

            # Fallback: don't hang forever once the doc is complete.
            if ready and loaded_at is not None and (time.time() - loaded_at) > 15 and (last.get("hasH1") or last.get("hasLies") or last.get("hasTrust")):
                return last

        if nudge_scroll:
            try:
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            except Exception:
                pass
        time.sleep(0.5)

    return last


def extract_parsed_signals(driver, url: str) -> dict | None:
    """Dispatch to the appropriate per-site extractor. Returns None on unknown URLs."""
    u = url.lower()
    try:
        if "bot.sannysoft.com" in u:
            return _extract_sannysoft(driver)
        if "areyouheadless" in u:
            return _extract_areyouheadless(driver)
        if "bot.incolumitas.com" in u:
            return _extract_incolumitas(driver)
        if "fingerprintjs.github.io" in u:
            return _extract_fingerprintjs(driver)
        if "cheiron.org" in u or "creepjs" in u:
            return _extract_creepjs(driver)
        if "pixelscan.net" in u:
            return _extract_pixelscan(driver)
        if "cloudflare-quic.com" in u:
            return _extract_cloudflare_quic(driver)
        if "cloudflare.com/cdn-cgi/trace" in u:
            return _extract_cloudflare_trace(driver)
        if "hcaptcha" in u:
            return _extract_hcaptcha_demo(driver)
        if "google.com/recaptcha" in u:
            return _extract_recaptcha_demo(driver)
        if "scamalytics.com" in u:
            return _extract_scamalytics(driver)
        if "ipqualityscore.com" in u:
            return _extract_ipqualityscore(driver)
        if "amiunique.org" in u:
            return _extract_amiunique(driver)
        if "walgreens.com" in u or "fiverr.com" in u:
            return _extract_perimeter_x_cookies(driver)
        if "turnstile.pages.dev" in u:
            return _extract_turnstile(driver)
    except Exception as exc:
        print(f"[probe] signal extraction failed for {url}: {type(exc).__name__}: {exc}")
    return None


def _browser_hygiene_signals(report: dict | None) -> dict:
    """Convert browser_report.hygiene into UI-friendly probe signals."""
    if not isinstance(report, dict):
        return {}
    hygiene = report.get("hygiene") if isinstance(report.get("hygiene"), dict) else {}
    binary = report.get("browser_binary") if isinstance(report.get("browser_binary"), dict) else {}
    navigator = report.get("navigator") if isinstance(report.get("navigator"), dict) else {}
    if not hygiene:
        return {}

    out = {}
    if binary.get("version"):
        out["browser.binary_version"] = _signal(binary.get("version"), None)
    if hygiene.get("browser_version"):
        out["browser.browser_version"] = _signal(hygiene.get("browser_version"), None)
    if hygiene.get("chromedriver_version"):
        out["browser.chromedriver_version"] = _signal(hygiene.get("chromedriver_version"), None)
    for key, label in (
        ("binary_browser_major_match", "binary vs browser major"),
        ("binary_browser_version_match", "binary vs browser exact version"),
        ("chromedriver_browser_major_match", "chromedriver vs browser major"),
        ("chromedriver_browser_build_match", "chromedriver vs browser build"),
        ("ua_browser_major_match", "UA vs browser major"),
        ("ua_ch_browser_major_match", "UA-CH vs browser major"),
    ):
        value = hygiene.get(key)
        if value is not None:
            out[f"browser.{key}"] = _signal(str(bool(value)).lower(), "passed" if value else "failed")

    exact_driver_match = hygiene.get("chromedriver_browser_version_match")
    if exact_driver_match is not None:
        out["browser.chromedriver_browser_version_match"] = _signal(
            str(bool(exact_driver_match)).lower(),
            "passed" if exact_driver_match else "warn",
        )

    headless_ua = bool(hygiene.get("headless_user_agent"))
    out["browser.headless_user_agent"] = _signal(str(headless_ua).lower(), "failed" if headless_ua else "passed")

    duplicate_window = bool(hygiene.get("duplicate_window_size_args"))
    window_sizes = hygiene.get("window_size_values") if isinstance(hygiene.get("window_size_values"), list) else []
    out["browser.duplicate_window_size_args"] = _signal(
        ", ".join(window_sizes) if window_sizes else "(none)",
        "failed" if duplicate_window else "passed",
    )

    rotated_profile = bool(hygiene.get("chrome_for_testing_uses_rotated_profile"))
    profile_dirs = hygiene.get("profile_directory_values") if isinstance(hygiene.get("profile_directory_values"), list) else []
    out["browser.chrome_for_testing_rotated_profile"] = _signal(
        ", ".join(profile_dirs) if profile_dirs else "(none)",
        "failed" if rotated_profile else "passed",
    )

    # Canonical automation primitive: navigator.webdriver
    webdriver_val = navigator.get("webdriver", None)
    if webdriver_val is not None:
        out["browser.navigator_webdriver"] = _signal(
            str(webdriver_val).lower() if isinstance(webdriver_val, bool) else str(webdriver_val),
            "failed" if bool(webdriver_val) else "passed",
        )
    desc = navigator.get("webdriver_descriptor")
    if isinstance(desc, dict):
        if "error" in desc:
            out["browser.webdriver_descriptor.error"] = _signal(str(desc.get("error") or ""), "warn")
        else:
            for scope, payload in (("prototype", desc.get("on_prototype")), ("instance", desc.get("on_instance"))):
                if not isinstance(payload, dict):
                    continue
                for key in ("configurable", "enumerable", "has_get", "has_value"):
                    if key in payload:
                        out[f"browser.webdriver_descriptor.{scope}.{key}"] = _signal(str(bool(payload.get(key))).lower(), None)
    return out


def _collect_har_lite(driver) -> list | None:
    """
    Collect a lightweight resource-timing HAR for the current page (≤200 entries).

    Fields per entry: url, initiator type, transfer/decoded size, duration ms,
    blocked flag (transferSize=0 + decodedBodySize=0 + duration<5 ms).
    """
    try:
        raw = driver.execute_script(r"""
        const entries = performance.getEntriesByType('resource') || [];
        return JSON.stringify(entries.slice(0, 200).map(function(e) {
          return {
            url:      String(e.name || '').slice(0, 200),
            init:     e.initiatorType || '',
            xfer:     e.transferSize,
            decoded:  e.decodedBodySize,
            dur_ms:   Math.round(e.duration),
            blocked:  (e.transferSize === 0 && e.decodedBodySize === 0 && e.duration < 5) ? 1 : 0
          };
        }));
        """)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def compute_probe_score(
    results: list,
    browser_report: dict | None = None,
    interaction_probe: dict | None = None,
) -> dict:
    """
    Compute a structured probe score across six categories.

    browser_hygiene      — navigator.webdriver, UA, binary/driver version parity
    js_fingerprint       — automation-leak detectors; infrastructure errors skipped
                           (a crashed window is a probe reliability issue, not a stealth failure)
    network_reputation   — TLS/QUIC negotiation, IP fraud score, ASN classification
    waf_trust            — enterprise WAF trust-state outcomes (highest weight)
    interaction_quality  — behavioral realism of press-and-hold gesture; excluded if
                           --interaction-test was not run
    probe_reliability    — infrastructure health: targets run, signals extracted, error count

    transport_architecture is emitted as a separate observational section not counted
    in overall/total (CDP is structural to chromedriver, not a stealth failure).

    Returns {"overall": N, "total": N, "pct": N, "categories": {...}, "transport_architecture": {...}}.
    """
    try:
        from re_analyzer.scrapers.probe_targets import PROBE_TARGETS, url_to_target_map
    except ImportError:
        return {}

    url_to_result: dict[str, dict] = {}
    for r in (results or []):
        if isinstance(r, dict) and r.get("url"):
            url_to_result[r["url"]] = r

    def _sig(r: dict, key: str) -> dict:
        return (r.get("parsed_signals") or {}).get(key) or {}

    def _val(r: dict, key: str, default: str = "") -> str:
        return str(_sig(r, key).get("value") or default).strip()

    def _st(r: dict, key: str) -> str | None:
        return _sig(r, key).get("status")

    def _crashed(r: dict) -> bool:
        """True if the result represents an infrastructure failure, not a detection event."""
        err = str(r.get("error") or "").lower()
        return bool(r.get("error")) and any(
            kw in err for kw in ("window", "session", "devtools", "timeout", "connection", "no such")
        )

    cats: dict[str, dict] = {
        "browser_hygiene":    {"name": "Browser Configuration Hygiene",    "score": 0, "max": 0, "items": []},
        "js_fingerprint":     {"name": "JS Fingerprint & Automation Leak", "score": 0, "max": 0, "items": []},
        "network_reputation": {"name": "Network / IP Reputation",          "score": 0, "max": 0, "items": []},
        "waf_trust":          {"name": "WAF Trust State",                  "score": 0, "max": 0, "items": []},
        "interaction_quality":{"name": "Interaction Quality",              "score": 0, "max": 0, "items": []},
        "probe_reliability":  {"name": "Probe Reliability",                "score": 0, "max": 0, "items": []},
    }

    def _add(cat: str, label: str, pts: int, max_pts: int, note: str = "") -> None:
        cats[cat]["score"] += pts
        cats[cat]["max"] += max_pts
        cats[cat]["items"].append({"label": label, "score": pts, "max": max_pts, "note": note})

    # ── browser_hygiene ───────────────────────────────────────────────────────
    blank = url_to_result.get("about:blank") or {}
    blank_ps = blank.get("parsed_signals") or {}
    for hk in (
        "browser.navigator_webdriver",
        "browser.headless_user_agent",
        "browser.binary_browser_major_match",
        "browser.chromedriver_browser_major_match",
        "browser.ua_browser_major_match",
        "browser.duplicate_window_size_args",
    ):
        st = (blank_ps.get(hk) or {}).get("status")
        _add("browser_hygiene", hk, 2 if st == "passed" else 0, 2, "" if st == "passed" else f"status={st}")

    # ── js_fingerprint ────────────────────────────────────────────────────────
    # Infrastructure errors (window crashes, session loss) skip the item entirely
    # so they are absorbed by probe_reliability rather than penalising stealth quality.

    cj = url_to_result.get("https://creepjs.org/checker") or {}
    if not _crashed(cj):
        lies_raw = _val(cj, "lies", "?")
        trust_raw = _val(cj, "trust_score", "0%").replace("%", "").strip()
        try:
            cj_pts = (10 if int(lies_raw) == 0 else 0) + (5 if float(trust_raw) >= 85 else 0)
        except Exception:
            cj_pts = 0
        _add("js_fingerprint", "CreepJS lies+trust", cj_pts, 15, f"lies={lies_raw} trust={trust_raw}%")

    inco = url_to_result.get("https://bot.incolumitas.com/") or {}
    if not _crashed(inco):
        inco_inc = _val(inco, "behavioralClassificationScore_incomplete")
        if inco_inc != "true":
            inco_num_raw = _val(inco, "behavioralClassificationScore", "0")
            try:
                inco_num = float(inco_num_raw)
                inco_pts = 8 if inco_num >= 0.75 else int(inco_num * 8)
            except Exception:
                inco_pts = 0
            _add("js_fingerprint", "Incolumitas behavioral", inco_pts, 8, f"score={inco_num_raw}")
        # behaviorally incomplete skips scoring but is NOT a crash — absorbed into note only

    for url_, label, pts in [
        ("https://bot.sannysoft.com/", "Sannysoft webdriver check", 4),
        ("https://arh.antoinevastel.com/bots/areyouheadless", "AreYouHeadless verdict", 4),
    ]:
        r_ = url_to_result.get(url_) or {}
        if not _crashed(r_):
            key_ = "webdriver" if "sannysoft" in url_ else "response_text"
            _add("js_fingerprint", label, pts if _st(r_, key_) == "passed" else 0, pts)

    fpjs = url_to_result.get("https://fingerprintjs.github.io/fingerprintjs/") or {}
    if not _crashed(fpjs):
        has_id = bool(_val(fpjs, "visitorId") or _val(fpjs, "visitorId_dom_fallback"))
        _add("js_fingerprint", "FingerprintJS visitorId present", 3 if has_id else 0, 3)

    # ── network_reputation ────────────────────────────────────────────────────
    cfq = url_to_result.get("https://cloudflare-quic.com/") or {}
    _add("network_reputation", "Cloudflare QUIC (h3/h2)", 4 if _st(cfq, "next_hop_protocol") == "passed" else 0, 4)

    scam = next((url_to_result[u] for u in url_to_result if "scamalytics.com" in u), {})
    try:
        scam_pts = 5 if int(_val(scam, "scamalytics.fraud_score", "100")) <= 25 else 0
        scam_note = f"score={_val(scam, 'scamalytics.fraud_score')}"
    except Exception:
        scam_pts = 0
        scam_note = "missing"
    _add("network_reputation", "Scamalytics IP fraud score ≤25", scam_pts, 5, scam_note)

    ip_clean = all(
        _val(inco, f"ip_api.{k}", "false") == "false"
        for k in ("is_datacenter", "is_proxy", "is_vpn", "is_tor")
    )
    _add("network_reputation", "IP not datacenter/proxy/VPN/TOR", 4 if ip_clean else 0, 4)

    # ── waf_trust (highest weight) ────────────────────────────────────────────
    for target in PROBE_TARGETS:
        if target.vector != "waf_challenge" or not target.scored:
            continue
        r_ = url_to_result.get(target.url)
        if not r_ or _crashed(r_):
            continue
        if "walgreens" in target.url or "fiverr" in target.url:
            waf_st = _val(r_, "perimeterx.waf_status", "")
            bdiff = r_.get("baseline_diff") or {}
            dims = r_.get("waf_dimensions") or {}
            if waf_st == "clean":
                pts, note = 15, "clean — sensor active, trust granted"
            elif waf_st == "sensor_active_no_trust":
                if bdiff.get("automation_missing_vs_baseline"):
                    pts = 5
                    note = "sensor active (no collector), trust withheld (baseline got _px3 — automation gap)"
                elif bdiff and "baseline_has_px3" in bdiff and not bdiff["baseline_has_px3"]:
                    pts = 12
                    note = "sensor active (no collector), deployment doesn't issue _px3 on homepage (baseline-confirmed policy)"
                else:
                    pts = 10
                    note = "sensor active (no collector), trust not yet granted (run --run-baseline to distinguish policy from automation gap)"
            elif waf_st == "collector_active_no_trust":
                if bdiff.get("automation_missing_vs_baseline"):
                    pts = 4
                    note = "collector active, server withheld trust (baseline got _px3 — server evaluated and rejected)"
                elif bdiff and "baseline_has_px3" in bdiff and not bdiff["baseline_has_px3"]:
                    pts = 10
                    note = "collector active, deployment withholds _px3 regardless (baseline-confirmed policy)"
                else:
                    pts = 6
                    note = "collector active, trust withheld by server (run --run-baseline to determine if automation-specific)"
            elif waf_st == "visible_challenge":
                pts, note = 0, "visible challenge"
            elif waf_st == "hard_block":
                pts, note = 0, "hard block (PXCR*)"
            else:
                pts, note = 0, waf_st or "sensor absent / unknown"
            if dims:
                dim_flags = " | ".join(
                    k for k in ("sensor_active", "collector_active", "trust_cookie_issued")
                    if dims.get(k)
                )
                iq_dim = dims.get("interaction_quality", "")
                if dim_flags:
                    note += f" [{dim_flags}]"
                if iq_dim and iq_dim not in ("no_probe", "pass"):
                    note += f" [iq={iq_dim}]"
            _add("waf_trust", target.label, pts, 15, note)
        elif "turnstile" in target.url:
            ts_st = _st(r_, "cf_turnstile_response")
            ts_val = _val(r_, "cf_turnstile_response", "")
            ts_pts = 10 if ts_st == "passed" else (3 if ts_val == "(pending_timeout)" else 0)
            _add("waf_trust", "Cloudflare Turnstile", ts_pts, 10, ts_val)

    # ── interaction_quality ───────────────────────────────────────────────────
    # Only scored when --interaction-test was run; excluded from total otherwise.
    if interaction_probe and not interaction_probe.get("error"):
        counts = interaction_probe.get("counts") or {}
        crit = counts.get("critical", 0)
        high = counts.get("high", 0)
        med = counts.get("medium", 0)
        low = counts.get("low", 0)
        summary = interaction_probe.get("summary") or {}
        pressure = summary.get("pointerdown_pressure")
        hold_ms = summary.get("hold_duration_ms") or 0

        if crit > 0:
            iq_pts = 0
            iq_note = f"critical violations: {crit}"
        elif high > 1:
            iq_pts = 4
            iq_note = f"multiple high violations: {high}"
        elif high == 1:
            iq_pts = 8
            iq_note = f"1 high violation"
        elif med >= 2:
            iq_pts = 10
            iq_note = f"{med} medium violations"
        elif med == 1:
            iq_pts = 12
            iq_note = f"1 medium violation"
        elif low > 0:
            iq_pts = 14
            iq_note = f"{low} low violations only"
        else:
            iq_pts = 15
            iq_note = "no anti-patterns"

        if pressure is not None:
            iq_note += f" | pressure={pressure}"
        if hold_ms:
            iq_note += f" | hold={hold_ms:.0f}ms"

        _add("interaction_quality", "Press-and-hold gesture realism", iq_pts, 15, iq_note)

    # ── probe_reliability ─────────────────────────────────────────────────────
    scored_auto = [t for t in PROBE_TARGETS if t.scored and not t.manual_interaction]
    n_targets = len(scored_auto)
    n_ran = sum(1 for t in scored_auto if t.url in url_to_result)
    n_signals = sum(
        1 for t in scored_auto
        if t.url in url_to_result and bool((url_to_result[t.url].get("parsed_signals") or {}))
    )
    n_infra_errors = sum(
        1 for t in scored_auto
        if t.url in url_to_result and _crashed(url_to_result[t.url])
    )

    _add("probe_reliability", f"Targets completed {n_ran}/{n_targets}",
         min(n_ran, n_targets), n_targets,
         "infrastructure failures counted separately below")
    _add("probe_reliability", f"Targets with signals {n_signals}/{max(n_ran, 1)}",
         n_signals, max(n_ran, 1))
    if n_infra_errors:
        _add("probe_reliability", f"Infrastructure errors (window/session crashes)",
             0, n_infra_errors,
             f"{n_infra_errors} target(s) failed due to driver/window loss — not a stealth detection event")

    overall = sum(c["score"] for c in cats.values())
    total = sum(c["max"] for c in cats.values())

    # ── transport_architecture (observational — excluded from score) ──────────
    ta_items: list = []

    def _ta(label: str, value: str, note: str = "") -> None:
        ta_items.append({"label": label, "value": value, "note": note})

    _ta(
        "transport_layer",
        "CDP (chromedriver)",
        "Structural: all chromedriver sessions use CDP. Not fixable by stealth patching alone.",
    )
    ps_url = url_to_result.get("https://pixelscan.net/bot-check") or {}
    cdp_note = _val(ps_url, "pixelscan.cdp_structural_note")
    cdp_tab = (_sig(ps_url, "pixelscan.tab.cdp.status").get("value")
               or _sig(ps_url, "pixelscan.tab.cdp.status").get("status") or "")
    if ps_url:
        _ta("pixelscan_cdp_tab", cdp_tab or "(not run)",
            cdp_note or "CDP tab — always detected in chromedriver; compare with DevTools-open baseline.")
    cfq2 = url_to_result.get("https://cloudflare-quic.com/") or {}
    _ta("http_version", _val(cfq2, "next_hop_protocol") or "(not run)",
        "negotiated protocol from Cloudflare QUIC")
    tls = url_to_result.get("https://tls.peet.ws/api/all") or {}
    ja4 = _val(tls, "ja4") or _val(tls, "ja4_hash") or "(not run)"
    _ta("tls_ja4_fingerprint", ja4, "server-side TLS fingerprint — should match real Chrome")

    return {
        "overall": overall,
        "total": total,
        "pct": round(100 * overall / total) if total else 0,
        "categories": cats,
        "transport_architecture": {
            "name": "Transport Architecture",
            "note": "Observational — not counted in overall score.",
            "items": ta_items,
        },
    }


_COLLECTOR_URL_RE = re.compile(
    r"sapi\.|/api/v\d|/api/collector|collector\.|/xhr/|/telemetry",
    re.IGNORECASE,
)
_PX_HOST_RE = re.compile(
    r"px-cloud\.net|pxchk\.net|perimeterx\.net|micpn\.com|px-cdn\.net|pxi\.pub",
    re.IGNORECASE,
)


def _waf_dimensions(
    result: dict,
    bdiff: dict | None = None,
    interaction_probe: dict | None = None,
) -> dict:
    """
    Compute per-result WAF trust dimensions for HUMAN/PerimeterX targets.

    Dimensions:
      sensor_active         — sensor script loaded and executed
      collector_active      — sensor made outbound collector XHR calls
      trust_cookie_issued   — _px3 present in cookie jar
      trust_granted         — waf_status == clean (cookie + no challenge)
      interaction_quality   — pass/warn/fail based on interaction probe antipatterns
      baseline_match        — probe waf_status == baseline waf_status (or "no_baseline")
    """
    sigs = result.get("parsed_signals") or {}
    har = result.get("har_lite") or []

    def _sval(key: str) -> str:
        return str((sigs.get(key) or {}).get("value") or "").strip()

    # sensor_active
    js_exec = _sval("perimeterx.js_executed").lower() == "true"
    post_js_exec = _sval("perimeterx.post_interaction_js_executed").lower() == "true"
    sensor_active = js_exec or post_js_exec

    # collector_active: prefer the parsed signal (set from JS resource timing in
    # _extract_perimeter_x_cookies); fall back to HAR scan for cross-validation
    collector_from_signal = _sval("perimeterx.px_collector_active").lower() == "true"
    collector_from_har = any(
        _PX_HOST_RE.search(e.get("url") or "")
        and _COLLECTOR_URL_RE.search(e.get("url") or "")
        and not e.get("blocked")
        for e in har
    )
    collector_active = collector_from_signal or collector_from_har

    # trust_cookie_issued
    trust_cookie_issued = "_px3" in _sval("perimeterx.cookies_present")

    # trust_granted
    trust_granted = _sval("perimeterx.waf_status") == "clean"

    # interaction_quality
    iq = "no_probe"
    if interaction_probe and not interaction_probe.get("error"):
        counts = interaction_probe.get("counts") or {}
        crit = counts.get("critical", 0)
        high = counts.get("high", 0)
        med = counts.get("medium", 0)
        if crit > 0 or high > 1:
            iq = "fail"
        elif high == 1 or med >= 2:
            iq = "warn"
        else:
            iq = "pass"

    # baseline_match
    if bdiff is not None:
        baseline_match = "yes" if bdiff.get("same_waf_status") else "no"
    else:
        baseline_match = "no_baseline"

    return {
        "sensor_active": sensor_active,
        "collector_active": collector_active,
        "trust_cookie_issued": trust_cookie_issued,
        "trust_granted": trust_granted,
        "interaction_quality": iq,
        "baseline_match": baseline_match,
    }


def _compute_baseline_diff(probe_result: dict, baseline_result: dict) -> dict:
    """
    Summarises what changed between the stealth probe and the no-stealth baseline
    for a single WAF target. Returns a flat dict suitable for direct JSON embedding.
    """
    def _waf_status(r: dict) -> str:
        sigs = r.get("parsed_signals") or {}
        return str((sigs.get("perimeterx.waf_status") or {}).get("value") or "")

    def _px_cookie_list(r: dict) -> list:
        sigs = r.get("parsed_signals") or {}
        present = (sigs.get("perimeterx.cookies_present") or {}).get("value") or ""
        if not present or present in ("none", "(none)"):
            return []
        return [c.strip() for c in str(present).split(",") if c.strip()]

    probe_waf = _waf_status(probe_result)
    baseline_waf = _waf_status(baseline_result)

    probe_cookies = _px_cookie_list(probe_result)
    baseline_cookies = _px_cookie_list(baseline_result)

    challenge_statuses = {"visible_challenge", "hard_block"}
    baseline_has_px3 = "_px3" in baseline_cookies
    probe_has_px3 = "_px3" in probe_cookies

    return {
        "same_waf_status": probe_waf == baseline_waf,
        "automation_only_missing_cookies": bool(baseline_cookies and not probe_cookies),
        "automation_only_challenge": bool(
            probe_waf in challenge_statuses and baseline_waf not in challenge_statuses
        ),
        "baseline_px_cookies": baseline_cookies,
        "probe_px_cookies": probe_cookies,
        # Explicit _px3 fields for direct report interpretation
        "baseline_has_px3": baseline_has_px3,
        "trust_cookie_expected_from_baseline": baseline_has_px3,
        "automation_missing_vs_baseline": baseline_has_px3 and not probe_has_px3,
    }


def _capture_storage_snapshot(driver) -> dict:
    """
    Synchronously captures localStorage/sessionStorage key counts and key names.
    Asynchronously captures IndexedDB database names and service worker scopes.
    Values are not captured — only keys — to keep snapshot size bounded.
    """
    out: dict = {}
    try:
        raw = driver.execute_script(r"""
        var snap = {};
        try {
            var lsKeys = [];
            for (var i = 0; i < localStorage.length; i++) lsKeys.push(localStorage.key(i));
            snap.localStorage_count = lsKeys.length;
            snap.localStorage_keys  = lsKeys.slice(0, 50);
        } catch(e) {}
        try {
            var ssKeys = [];
            for (var i = 0; i < sessionStorage.length; i++) ssKeys.push(sessionStorage.key(i));
            snap.sessionStorage_count = ssKeys.length;
            snap.sessionStorage_keys  = ssKeys.slice(0, 50);
        } catch(e) {}
        return JSON.stringify(snap);
        """)
        if raw:
            out.update(json.loads(raw))
    except Exception:
        pass
    try:
        raw2 = driver.execute_async_script(r"""
        var done = arguments[arguments.length - 1];
        var out2 = {};
        var tasks = [];
        if (typeof indexedDB !== 'undefined' && typeof indexedDB.databases === 'function') {
            tasks.push(indexedDB.databases().then(function(dbs) {
                out2.indexedDB_names = dbs.map(function(d) { return d.name; });
            }).catch(function() {}));
        }
        if (navigator.serviceWorker) {
            tasks.push(navigator.serviceWorker.getRegistrations().then(function(regs) {
                out2.service_worker_scopes = regs.map(function(r) { return r.scope; });
            }).catch(function() {}));
        }
        Promise.all(tasks)
            .then(function() { done(JSON.stringify(out2)); })
            .catch(function() { done('{}'); });
        """)
        if raw2:
            out.update(json.loads(raw2))
    except Exception:
        pass
    return out


_WALGREENS_FUNNEL_STEPS: list = [
    ("homepage",      "https://www.walgreens.com/"),
    ("search",        "https://www.walgreens.com/search/results.jsp?Ntt=vitamins"),
    ("product",       "https://www.walgreens.com/store/c/vitamins-supplements/ID=360530-tier2"),
    ("store_locator", "https://www.walgreens.com/storelocator/find.jsp"),
    ("account",       "https://www.walgreens.com/login.jsp"),
]


def _run_px_funnel(driver_config, funnel_steps: list | None = None) -> list:
    """
    Visits a sequence of pages within a single persistent browser session to
    probe session-level trust-cookie buildup in HUMAN/PerimeterX deployments.

    Returns a list of per-step result dicts:
      {step, url, parsed_signals, storage_pre, storage_post, har_lite}
    or {step, url, error} on failure.
    """
    if funnel_steps is None:
        funnel_steps = _WALGREENS_FUNNEL_STEPS
    if not funnel_steps:
        return []

    step_results: list = []
    first_url = funnel_steps[0][1]
    try:
        with get_selenium_driver(first_url, driver_config=driver_config) as driver:
            for idx, (step_id, url) in enumerate(funnel_steps):
                try:
                    if idx > 0:
                        driver.get(url)
                        time.sleep(3.0)

                    storage_pre = _capture_storage_snapshot(driver)

                    try:
                        mouse = HumanMouse(driver)
                        mouse.wander(duration=2.0, scroll_probability=0.25, scroll_distance_range=(50, 150))
                    except Exception:
                        pass
                    time.sleep(2.5)

                    storage_post = _capture_storage_snapshot(driver)
                    signals = extract_parsed_signals(driver, url) or {}
                    har = _collect_har_lite(driver)

                    step_results.append({
                        "step": step_id,
                        "url": url,
                        "parsed_signals": signals or None,
                        "storage_pre": storage_pre or None,
                        "storage_post": storage_post or None,
                        "har_lite": har,
                    })
                except Exception as exc:
                    step_results.append({"step": step_id, "url": url, "error": str(exc)})
    except Exception as exc:
        step_results.append({"step": "session_init", "url": first_url, "error": str(exc)})

    return step_results


# ─── Main probe runner ────────────────────────────────────────────────────────

def run_probe(urls, output_dir: Path, headless=True, chrome_path=None, chromedriver_path=None, interaction_test=False, run_baseline=False, run_funnel=False):
    output_dir.mkdir(parents=True, exist_ok=True)
    probe_user_data_dir = (os.environ.get("RE_ANALYZER_PROBE_USER_DATA_DIR") or "").strip() or None
    probe_profile_directory = (os.environ.get("RE_ANALYZER_PROBE_PROFILE_DIRECTORY") or "").strip() or None
    probe_manual_wait_raw = str(os.environ.get("RE_ANALYZER_PROBE_MANUAL_CHALLENGE_WAIT_SECONDS", "0") or "").strip().lower()
    try:
        if probe_manual_wait_raw in {"none", "inf", "infinite"}:
            probe_manual_wait_seconds = None
        else:
            probe_manual_wait_seconds = float(probe_manual_wait_raw)
    except Exception:
        probe_manual_wait_seconds = 0.0
    driver_config = DriverConfig(
        browser_executable_path=chrome_path or CHROME_BINARY_EXECUTABLE_PATH,
        chromedriver_executable_path=chromedriver_path or CHROMEDRIVER_EXECUTABLE_PATH,
        user_data_dir=probe_user_data_dir,
        profile_directory=probe_profile_directory,
        headless=headless,
        ignore_detection=False,
        random_profile=False,
        clean_profile=False,
        manual_challenge_wait_seconds=probe_manual_wait_seconds,
    )
    if probe_user_data_dir:
        print(f"[probe] using configured probe user data dir: {probe_user_data_dir}", flush=True)
    else:
        print("[probe] using isolated temporary browser profile", flush=True)

    results = []
    known_public_ip = None
    run_browser_report = None
    interaction_probe_result = None

    if interaction_test:
        print("[probe] running interaction quality probe first", flush=True)
        try:
            from re_analyzer.scrapers.interaction_probe import run_interaction_probe
            with get_selenium_driver("about:blank", driver_config=driver_config) as idriver:
                interaction_probe_result = run_interaction_probe(idriver)
        except Exception as exc:
            print(f"[probe] interaction probe error: {exc}", flush=True)
            interaction_probe_result = {"error": str(exc)}

    for url in urls:
        if _stop_flag.is_set():
            print("[probe] stop flag set — skipping remaining URLs", flush=True)
            break
        resolved_url = url
        # Several IP-lookup pages only return meaningful content when an IP is
        # embedded in the URL path. Rewrite these using the egress IP learned from
        # an earlier probe target (ipinfo.io appears before both in probe order).
        try:
            if (
                known_public_ip
                and isinstance(url, str)
                and "ipqualityscore.com" in url.lower()
                and "/free-ip-lookup-proxy-vpn-test/lookup/" in url.lower()
                and re.search(r"/lookup/?$", url)
            ):
                resolved_url = url.rstrip("/") + f"/{known_public_ip}"
            elif (
                known_public_ip
                and isinstance(url, str)
                and "scamalytics.com/ip" in url.lower()
                and re.search(r"/ip/?$", url)
            ):
                resolved_url = url.rstrip("/") + f"/{known_public_ip}"
        except Exception:
            resolved_url = url

        print(f"[probe] visiting {resolved_url}")
        try:
            with get_selenium_driver(resolved_url, driver_config=driver_config) as driver:
                time.sleep(3)

                # Extra wait for sites with async rendering
                extra = next((v for k, v in _EXTRA_WAIT_SECONDS.items() if k in resolved_url.lower()), 0)
                if extra:
                    print(f"[probe] waiting extra {extra}s for async rendering: {resolved_url}")
                    if "bot.incolumitas.com" in resolved_url.lower():
                        # Inject behavioral events during the scoring window so the
                        # page has mouse/scroll data to classify (score stays "..." otherwise).
                        # Run for (extra - 2)s, then poll up to 8s for the server to
                        # return the classification and update #behavioralScore.
                        _simulate_incolumitas_behavioral(driver, duration_seconds=max(1.0, float(extra) - 2.0))
                        for _ in range(24):
                            try:
                                score_text = driver.execute_script(
                                    "var el = document.getElementById('behavioralScore');"
                                    "return el ? (el.innerText || el.textContent || '').trim() : '';"
                                ) or ""
                                if score_text and score_text != "...":
                                    break
                            except Exception:
                                pass
                            time.sleep(0.5)
                    else:
                        time.sleep(extra)

                amiunique_wait_status = None
                if "amiunique.org/fingerprint" in resolved_url.lower():
                    status = _wait_for_amiunique_render(driver, headless=bool(driver_config.headless))
                    amiunique_wait_status = status if isinstance(status, dict) else None
                    if isinstance(status, dict):
                        print(
                            "[probe] amiunique render wait: "
                            f"loadingCards={status.get('loadingCards')}, "
                            f"progressRows={status.get('progressRows')}, "
                            f"httpRows={status.get('httpRows')}, "
                            f"jsRows={status.get('jsRows')}, "
                            f"hasUniqueLine={status.get('hasUniqueLine')}, "
                            f"hasToplineLabels={status.get('hasToplineLabels')}, "
                            f"hasPct={status.get('hasPct')}",
                            flush=True,
                        )

                creepjs_wait_status = None
                if "creepjs.org" in resolved_url.lower():
                    status = _wait_for_creepjs_render(driver, headless=bool(driver_config.headless))
                    creepjs_wait_status = status if isinstance(status, dict) else None
                    if isinstance(status, dict):
                        print(
                            "[probe] creepjs render wait: "
                            f"readyState={status.get('readyState')}, "
                            f"hasH1={status.get('hasH1')}, "
                            f"analyzing={status.get('analyzing')}, "
                            f"hasLies={status.get('hasLies')}, "
                            f"hasTrust={status.get('hasTrust')}",
                            flush=True,
                        )

                report = collect_browser_report(driver=driver, driver_config=driver_config)
                if run_browser_report is None and isinstance(report, dict):
                    run_browser_report = report

                challenge = detect_challenge(driver)
                print(f"[probe] result for {resolved_url}: is_challenge={challenge['is_challenge']}, matched={challenge['matched_patterns']}")

                # CDP press-and-hold is only meaningful for PerimeterX challenges.
                # For hCaptcha, reCAPTCHA, and Cloudflare interactive challenges the
                # auto-attempt would be a no-op at best and confusing at worst.
                _PX_AUTO_PATTERNS = {"press_and_hold", "px_captcha", "perimeterx_instrumentation"}
                _matched = set(challenge.get("matched_patterns") or [])
                _can_auto_resolve = bool(_matched & _PX_AUTO_PATTERNS)

                challenge_resolution = None
                if challenge.get("is_challenge") and _can_auto_resolve:
                    wait_seconds = getattr(driver_config, "manual_challenge_wait_seconds", 0)
                    print(f"[probe] PX challenge detected — attempting CDP resolution (manual_wait={wait_seconds}s)", flush=True)
                    resolved_challenge = wait_for_manual_challenge(driver, wait_seconds)
                    challenge_resolution = {
                        "attempted": True,
                        "cleared": not resolved_challenge.get("is_challenge"),
                        "pre_patterns": challenge.get("matched_patterns", []),
                        "post_patterns": resolved_challenge.get("matched_patterns", []),
                    }
                    challenge = resolved_challenge
                    print(
                        f"[probe] post-resolution for {resolved_url}: "
                        f"cleared={challenge_resolution['cleared']}, matched={challenge['matched_patterns']}",
                        flush=True,
                    )

                filename = resolved_url.replace('://', '_').replace('/', '_').replace('?', '_')

                # Capture JSON response body for JSON content-type endpoints
                echo_json = None
                try:
                    content_type = driver.execute_script("return document.contentType;") or ""
                except Exception:
                    content_type = ""
                if "json" in str(content_type).lower():
                    try:
                        body_text = driver.execute_script(
                            "return document.body ? document.body.innerText : '';"
                        ) or ""
                        if body_text and len(body_text) <= 200_000:
                            echo_json = json.loads(body_text)
                    except Exception:
                        echo_json = None

                # Site-specific signal extraction from the rendered DOM
                parsed_signals = extract_parsed_signals(driver, resolved_url) or {}
                parsed_signals.update(_browser_hygiene_signals(report))
                if isinstance(amiunique_wait_status, dict):
                    # Surface wait-state telemetry so UI users can tell when a capture was mid-load.
                    for k in ("loadingCards", "progressRows", "httpRows", "jsRows", "hasUniqueLine", "hasToplineLabels", "hasPct"):
                        if k in amiunique_wait_status:
                            parsed_signals[f"amiunique.render_wait.{k}"] = _signal(str(amiunique_wait_status.get(k)), None)
                if isinstance(creepjs_wait_status, dict):
                    for k in ("readyState", "hasH1", "analyzing", "challengeLike", "hasLies", "hasTrust"):
                        if k in creepjs_wait_status:
                            parsed_signals[f"creepjs.render_wait.{k}"] = _signal(str(creepjs_wait_status.get(k)), None)
                if parsed_signals:
                    print(f"[probe] extracted {len(parsed_signals)} signals from {resolved_url}")

                # Update known_public_ip for later targets (e.g. IPQualityScore auto-lookup).
                try:
                    candidate = None
                    if isinstance(echo_json, dict):
                        candidate = echo_json.get("ip") or echo_json.get("query") or echo_json.get("origin") or None
                    if not candidate and isinstance(parsed_signals, dict):
                        candidate = (
                            (parsed_signals.get("ip_api.ip") or {}).get("value")
                            or (parsed_signals.get("cloudflare_trace.ip") or {}).get("value")
                            or None
                        )
                    if candidate:
                        candidate = str(candidate).strip()
                        # httpbin may return a comma-delimited origin chain like
                        # "client_ip, proxy_ip". Prefer the first token.
                        if "," in candidate:
                            candidate = candidate.split(",", 1)[0].strip()
                        try:
                            ipaddress.ip_address(candidate)
                            if known_public_ip != candidate:
                                known_public_ip = candidate
                                print(f"[probe] learned egress IP: {known_public_ip}", flush=True)
                        except ValueError:
                            pass
                except Exception:
                    pass

                # HAR-lite: lightweight resource timing for every URL
                har_lite = _collect_har_lite(driver)

                # Full browser console log grouped by severity
                console_log: list = []
                try:
                    raw_logs = driver.get_log("browser") or []
                    for entry in raw_logs:
                        lvl = str(entry.get("level") or "")
                        console_log.append({
                            "level": lvl,
                            "msg": str(entry.get("message") or "")[:500],
                            "ts": entry.get("timestamp"),
                        })
                except Exception:
                    pass

                extra_meta = {"url": resolved_url, "browser_report": report}
                if isinstance(echo_json, (dict, list)):
                    extra_meta["echo_json"] = echo_json
                if parsed_signals:
                    extra_meta["parsed_signals"] = parsed_signals

                diagnostics = save_page_diagnostics(
                    driver,
                    output_dir,
                    f"probe_{filename}",
                    extra=extra_meta,
                )
                diagnostics_error = None
                if isinstance(diagnostics, dict):
                    if diagnostics.get("html_error") or diagnostics.get("screenshot_error"):
                        diagnostics_error = diagnostics.get("html_error") or diagnostics.get("screenshot_error")
                _result_entry: dict = {
                    "url": resolved_url,
                    "original_url": url if resolved_url != url else None,
                    "challenge": challenge,
                    "challenge_resolution": challenge_resolution,
                    "content_type": content_type,
                    "echo_json": echo_json if isinstance(echo_json, (dict, list)) else None,
                    "parsed_signals": parsed_signals or None,
                    "har_lite": har_lite,
                    "console_log": console_log or None,
                    "error": diagnostics_error,
                    "diagnostics": diagnostics,
                }
                # For WAF targets, add initial dimensions (no bdiff yet — refreshed later if baseline runs)
                try:
                    from re_analyzer.scrapers.probe_targets import VECTOR_WAF_CHALLENGE, url_to_target_map as _utm
                    _tmap = _utm()
                    _t = _tmap.get(resolved_url) or _tmap.get(url)
                    if _t and _t.vector == VECTOR_WAF_CHALLENGE:
                        _result_entry["waf_dimensions"] = _waf_dimensions(
                            _result_entry,
                            interaction_probe=interaction_probe_result,
                        )
                except Exception:
                    pass
                results.append(_result_entry)
        except Exception as exc:
            print(f"[probe] error accessing {resolved_url}: {type(exc).__name__}: {exc}")
            results.append({"url": resolved_url, "original_url": url if resolved_url != url else None, "error": str(exc)})

    # Baseline comparison: second pass with no-stealth config restricted to WAF URLs.
    # Each main result gets a "baseline" sub-dict so the UI can diff trust-cookie and
    # challenge outcomes between the stealth and plain-browser contexts.
    baseline_results: list = []
    if run_baseline:
        try:
            from re_analyzer.scrapers.probe_targets import PROBE_TARGETS, VECTOR_WAF_CHALLENGE
        except ImportError:
            PROBE_TARGETS, VECTOR_WAF_CHALLENGE = [], "waf_challenge"

        waf_urls = [t.url for t in PROBE_TARGETS if t.vector == VECTOR_WAF_CHALLENGE and t.scored]
        waf_urls_to_run = [u for u in waf_urls if any(r.get("url") == u for r in results)]
        if waf_urls_to_run:
            print(f"[probe] running baseline (no-stealth) pass for {len(waf_urls_to_run)} WAF URLs", flush=True)
            baseline_config = DriverConfig(
                browser_executable_path=driver_config.browser_executable_path,
                chromedriver_executable_path=driver_config.chromedriver_executable_path,
                user_data_dir=None,
                profile_directory=None,
                headless=driver_config.headless,
                ignore_detection=True,
                random_profile=False,
                clean_profile=True,
                manual_challenge_wait_seconds=0.0,
            )
            for url in waf_urls_to_run:
                if _stop_flag.is_set():
                    break
                print(f"[probe] baseline visiting {url}", flush=True)
                try:
                    with get_selenium_driver(url, driver_config=baseline_config) as bdriver:
                        time.sleep(5)
                        bsignals = extract_parsed_signals(bdriver, url) or {}
                        bhar = _collect_har_lite(bdriver)
                        baseline_results.append({
                            "url": url,
                            "parsed_signals": bsignals or None,
                            "har_lite": bhar,
                        })
                except Exception as exc:
                    baseline_results.append({"url": url, "error": str(exc)})

        # Attach baseline sub-dict, diff summary, and refreshed WAF dimensions
        baseline_by_url = {r.get("url"): r for r in baseline_results if isinstance(r, dict)}
        for r in results:
            if isinstance(r, dict) and r.get("url") in baseline_by_url:
                b = baseline_by_url[r["url"]]
                r["baseline"] = b
                if not b.get("error"):
                    r["baseline_diff"] = _compute_baseline_diff(r, b)
                    # Baseline gets its own waf_dimensions for direct side-by-side comparison
                    b["waf_dimensions"] = _waf_dimensions(b)
                    # Refresh probe dimensions now that bdiff is available
                    if r.get("waf_dimensions"):
                        r["waf_dimensions"] = _waf_dimensions(
                            r, bdiff=r["baseline_diff"],
                            interaction_probe=interaction_probe_result,
                        )

    funnel_results: list = []
    if run_funnel and not _stop_flag.is_set():
        print("[probe] running multi-page funnel probe (session-level trust buildup)", flush=True)
        try:
            funnel_results = _run_px_funnel(driver_config)
        except Exception as exc:
            funnel_results = [{"step": "funnel_error", "error": str(exc)}]

    return results, run_browser_report, interaction_probe_result, funnel_results


def parse_args():
    parser = argparse.ArgumentParser(description="Probe public challenge pages using the shared driver config.")
    parser.add_argument("--urls", nargs="*", default=DEFAULT_TEST_URLS, help="URLs to probe.")
    parser.add_argument("--output-dir", default=str(Path(DATA_PATH) / "DetectionProbe"), help="Directory where diagnostics are saved.")
    parser.add_argument("--no-headless", action="store_true", help="Run the browser with a visible window.")
    parser.add_argument("--chrome-path", default="", help="Optional Chrome binary path override.")
    parser.add_argument("--chromedriver-path", default="", help="Optional chromedriver binary path override.")
    parser.add_argument("--interaction-test", action="store_true", help="Run interaction quality probe to check press-and-hold anti-patterns.")
    parser.add_argument(
        "--run-baseline", action="store_true",
        help=(
            "Run a second pass with a no-stealth Chrome profile restricted to WAF targets "
            "(Walgreens/Fiverr/Turnstile) and attach the baseline signals to each result for "
            "side-by-side comparison. Adds ~30 s to the probe run."
        ),
    )
    parser.add_argument(
        "--waf-funnel", action="store_true",
        help=(
            "Run the multi-page Walgreens funnel probe in a single persistent browser session "
            "to capture session-level trust-cookie buildup, storage deltas, and per-step network "
            "activity. Results are emitted in funnel_results. Adds ~60 s to the probe run."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    output_dir = Path(args.output_dir)
    started_at = datetime.now(timezone.utc)
    chrome_path = str(args.chrome_path or "").strip() or None
    chromedriver_path = str(getattr(args, "chromedriver_path", "") or "").strip() or None
    results, browser_report, interaction_probe_result, funnel_results = run_probe(
        args.urls,
        output_dir,
        headless=not args.no_headless,
        chrome_path=chrome_path,
        chromedriver_path=chromedriver_path,
        interaction_test=bool(args.interaction_test),
        run_baseline=bool(getattr(args, "run_baseline", False)),
        run_funnel=bool(getattr(args, "waf_funnel", False)),
    )
    completed_at = datetime.now(timezone.utc)

    probe_score = {}
    try:
        probe_score = compute_probe_score(results, browser_report, interaction_probe=interaction_probe_result)
    except Exception:
        pass

    urls_run = [r["url"] for r in results if isinstance(r, dict) and r.get("url")]
    summary = {
        "kind": "probe",
        "partial": _stop_flag.is_set(),
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": round((completed_at - started_at).total_seconds(), 2),
        "headless": not args.no_headless,
        "chrome_path": chrome_path,
        "output_dir": str(output_dir),
        "urls": list(args.urls),
        "urls_completed": urls_run,
        "browser_report": browser_report,
        "interaction_probe": interaction_probe_result,
        "probe_score": probe_score,
        "results": results,
        "funnel_results": funnel_results or None,
        "errors": [item for item in results if isinstance(item, dict) and item.get("error")],
    }
    print("PROBE_RUN_SUMMARY")
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
