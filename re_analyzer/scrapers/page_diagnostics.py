import json
import hashlib
import os
import random
import re
import shutil
import time
from datetime import datetime
from pathlib import Path


SOURCE_CHALLENGE_PATTERNS = [
    # PerimeterX / HUMAN.
    # Note: instrumentation presence alone does not imply a blocking challenge; many sites
    # load PerimeterX scripts on normal pages. We keep that as a separate tag so the UI
    # can show it without promoting it to a "soft challenge" by itself.
    ("perimeterx_instrumentation", re.compile(r"\\bperimeterx\\b|window\\._px(?:Uuid|AppId|Vid)\\b|\\b_px(?:uuid|app_id|vid)\\b", re.IGNORECASE)),
    ("px_captcha", re.compile(r"id=[\"']px-captcha(?:-[^\"']+)?[\"']|px-captcha-(?:wrapper|container|modal)|\\bPXCR\\d+\\b", re.IGNORECASE)),
    ("recaptcha_frame", re.compile(r"recaptcha/api2/(?:anchor|bframe|aframe)|google\\.com/recaptcha|google_captcha_public_key", re.IGNORECASE)),
    ("hcaptcha_frame", re.compile(r"iframe[^>]+src=[\"'][^\"']*hcaptcha\.com|h-captcha", re.IGNORECASE)),
    # Cloudflare challenges (avoid matching pages that merely include Turnstile or bot-manager scripts).
    ("cloudflare_challenge", re.compile(r"cf-chl-bypass|cf_captcha_kind|checking your browser before accessing|ddos protection by cloudflare|attention required|cf_chl_", re.IGNORECASE)),
    # Realtor.com block page: contact email is always present in raw HTML even when the visible
    # text uses color:transparent animation to hide it from innerText-based checks.
    ("realtor_block", re.compile(r"unblockrequest@realtor\.com", re.IGNORECASE)),
]

VISIBLE_CHALLENGE_PATTERNS = [
    ("press_and_hold", re.compile(r"press\s*(?:&|and)\s*hold|verify\s+you\s+are\s+(?:a\s+)?human", re.IGNORECASE)),
    ("perimeterx_block", re.compile(r"it needs a human touch|complete the task and we['’]?ll get you right back|ERRCODE\s*PXCR\d+|\bPXCR\d+\b", re.IGNORECASE)),
    ("robot_check", re.compile(r"not\s+a\s+bot|unusual\s+traffic|automated\s+requests|are\s+you\s+a\s+robot", re.IGNORECASE)),
    # Avoid matching generic words like "blocked" in normal content (false positives).
    # Prefer explicit interstitial phrasing.
    ("access_denied", re.compile(
        r"\b(?:"
        r"access\s+(?:to\s+this\s+page\s+has\s+been\s+)?denied"
        r"|forbidden"
        r"|you\s+have\s+been\s+blocked"
        r"|this\s+(?:request|page)\s+has\s+been\s+blocked"
        r"|request\s+blocked"
        r")\b",
        re.IGNORECASE,
    )),
    ("request_blocked", re.compile(r"your request could not be processed|unblockrequest@realtor\.com|kpsdk", re.IGNORECASE)),
    ("hcaptcha", re.compile(r"hcaptcha|h-captcha", re.IGNORECASE)),
    ("cloudflare_challenge", re.compile(r"checking your browser before accessing|attention required|ddos protection by cloudflare|please enable cookies|are you human|checking your browser", re.IGNORECASE)),
]

USABLE_LISTING_PATTERNS = [
    re.compile(r"\b(?:homes?\s+for\s+sale|real\s+estate)\b", re.IGNORECASE),
    re.compile(r"\b\d{1,4}\s+homes?\b", re.IGNORECASE),
    re.compile(r"\$[\d,]{3,}", re.IGNORECASE),
    re.compile(r"\b\d+\s+(?:bd|bds|bed|beds)\b", re.IGNORECASE),
]


def page_text(driver, max_chars=4000):
    try:
        text = driver.execute_script("return document.body ? document.body.innerText : '';") or ""
    except Exception:
        text = ""
    return text[:max_chars]


def page_source_excerpt(driver, max_chars=6000):
    try:
        source = driver.page_source or ""
    except Exception:
        source = ""
    return source[:max_chars]

def _safe_execute_script(driver, script, default=None):
    try:
        return driver.execute_script(script)
    except Exception:
        return default


def _dom_source_matches(driver):
    """
    Lightweight challenge detection via DOM inspection.

    This avoids pulling the full page HTML (driver.page_source) on every poll
    during manual challenge waits.
    """
    matches = set()
    try:
        payload = driver.execute_script(
            """
                const out = { px_captcha: false, px_instrumentation: false, recaptcha: false, hcaptcha: false, cloudflare: false, realtor_block: false };
            try {
              const hasPxCaptcha =
                Boolean(document.getElementById("px-captcha-wrapper")) ||
                Boolean(document.getElementById("px-captcha-modal")) ||
                Boolean(document.querySelector("#px-captcha, .px-captcha-container, .px-captcha-error-container")) ||
                Boolean(document.querySelector("[id^='px-captcha-'], [class*='px-captcha']"));
              out.px_captcha = hasPxCaptcha;
              out.px_instrumentation = Boolean(window._pxUuid || window._pxAppId || window._pxVid);
            } catch (e) {}
            try {
              out.recaptcha = Boolean(document.querySelector("iframe[src*='recaptcha/api2/'], iframe[src*='google.com/recaptcha/']"));
            } catch (e) {}
            try {
              out.hcaptcha = Boolean(document.querySelector("iframe[src*='hcaptcha.com/'], .h-captcha, .hcaptcha"));
            } catch (e) {}
            try {
              // Cloudflare *hard* interstitial markers.
              // Do not treat generic Turnstile widget embeds as a blocking challenge.
              out.cloudflare = Boolean(
                document.querySelector("#cf-challenge-running, #challenge-form, iframe[src*='challenges.cloudflare.com/']") ||
                Boolean(document.querySelector("input[name='cf_captcha_kind'], input[name='cf_challenge_response']"))
              );
            } catch (e) {}
            try {
              // Realtor.com CSS-obfuscated block page: animation:reveal hides text from innerText.
              // The .hp class (honeypot off-screen spans) combined with animation:reveal is a
              // distinctive fingerprint not found on normal listing pages.
              const styleContent = Array.from(document.querySelectorAll('style'))
                .map(s => s.textContent || '').join('');
              const stripped = styleContent.replace(/\\s/g, '');
              out.realtor_block = (
                (stripped.includes('animation:reveal') || stripped.includes('@keyframesreveal')) &&
                stripped.includes('.hp{')
              );
            } catch (e) {}
            return out;
            """
        ) or {}
    except Exception:
        payload = {}
    if payload.get("px_captcha"):
        matches.add("px_captcha")
    if payload.get("px_instrumentation"):
        matches.add("perimeterx_instrumentation")
    if payload.get("recaptcha"):
        matches.add("recaptcha_frame")
    if payload.get("hcaptcha"):
        matches.add("hcaptcha_frame")
    if payload.get("cloudflare"):
        matches.add("cloudflare_challenge")
    if payload.get("realtor_block"):
        matches.add("realtor_block")
    return matches


def _position_cursor_for_hil(driver):
    """
    Best-effort HIL preparation to make manual interaction easier.

    This is not a bypass mechanism; it does not click or hold. It only attempts
    to scroll and visually highlight the most likely interactive control (e.g.,
    a PerimeterX "Press & Hold" button) so a human can click more easily.
    """
    try:
        from selenium.webdriver.common.by import By
    except Exception:
        return None, {"positioned": False, "reason": "selenium_unavailable"}

    selectors = [
        "#px-captcha",
        ".px-captcha-container",
        "#px-captcha-wrapper .px-captcha-error-button",
        ".px-captcha-error-button",
        "#px-captcha-wrapper",
        "[id^='px-captcha-'] .px-captcha-error-button",
        "[id^='px-captcha-']",
        "iframe[src*='recaptcha/api2/anchor']",
        "iframe[src*='recaptcha/api2/']",
        "iframe[src*='google.com/recaptcha']",
        ".g-recaptcha",
    ]

    element = None
    selector_used = None
    for selector in selectors:
        try:
            candidate = driver.find_element(By.CSS_SELECTOR, selector)
            is_visible = True
            try:
                is_visible = bool(
                    driver.execute_script(
                        """
                        try {
                          const el = arguments[0];
                          if (!el || !el.getBoundingClientRect) return false;
                          const rect = el.getBoundingClientRect();
                          const style = window.getComputedStyle(el);
                          const opacity = style ? parseFloat(style.opacity || "1") : 1;
                          const ok =
                            Boolean(style) &&
                            style.display !== "none" &&
                            style.visibility !== "hidden" &&
                            opacity > 0.01 &&
                            rect.width >= 8 &&
                            rect.height >= 8;
                          return ok;
                        } catch (e) {
                          return true;
                        }
                        """,
                        candidate,
                    )
                )
            except Exception:
                is_visible = True
            if not is_visible:
                continue
            element = candidate
            selector_used = selector
            break
        except Exception:
            continue

    if element is None:
        try:
            element = driver.find_element(By.TAG_NAME, "body")
            selector_used = "body"
        except Exception:
            return None, {"positioned": False, "reason": "no_element"}

    try:
        driver.execute_script(
            "try { arguments[0].scrollIntoView({block:'center',inline:'center'}); } catch (e) {}",
            element,
        )
    except Exception:
        pass

    try:
        try:
            driver.execute_script("try { window.focus(); } catch (e) {}")
        except Exception:
            pass
        try:
            refined = driver.execute_script(
                """
                try {
                  const el = arguments[0];
                  if (!el || !el.getBoundingClientRect) return el;
                  const rect = el.getBoundingClientRect();
                  const cx = rect.left + rect.width / 2;
                  const cy = rect.top + rect.height / 2;
                  const x = Math.min(Math.max(0, cx), Math.max(0, window.innerWidth - 1));
                  const y = Math.min(Math.max(0, cy), Math.max(0, window.innerHeight - 1));
                  return document.elementFromPoint(x, y) || el;
                } catch (e) {
                  return arguments[0];
                }
                """,
                element,
            )
            if refined:
                element = refined
                if selector_used:
                    selector_used = f"{selector_used} -> elementFromPoint"
        except Exception:
            pass
        rect = None
        try:
            rect = driver.execute_script(
                """
                try {
                  const el = arguments[0];
                  if (!el || !el.getBoundingClientRect) return null;
                  const r = el.getBoundingClientRect();
                  return {
                    left: r.left,
                    top: r.top,
                    width: r.width,
                    height: r.height,
                    cx: r.left + (r.width / 2),
                    cy: r.top + (r.height / 2),
                    viewport: { width: window.innerWidth, height: window.innerHeight, dpr: window.devicePixelRatio || 1 },
                  };
                } catch (e) {
                  return null;
                }
                """,
                element,
            )
        except Exception:
            rect = None
        try:
            driver.execute_script(
                """
                try {
                  const el = arguments[0];
                  const id = "__re_analyzer_hil_marker__";
                  const rect = el && el.getBoundingClientRect ? el.getBoundingClientRect() : null;
                  if (!rect) return;

                  el.style.outline = "3px solid rgba(255, 0, 80, 0.95)";
                  el.style.outlineOffset = "2px";

                  let marker = document.getElementById(id);
                  if (!marker) {
                    marker = document.createElement("div");
                    marker.id = id;
                    marker.style.position = "fixed";
                    marker.style.zIndex = "2147483647";
                    marker.style.pointerEvents = "none";
                    marker.style.border = "3px solid rgba(255, 0, 80, 0.95)";
                    marker.style.borderRadius = "8px";
                    marker.style.background = "rgba(255, 0, 80, 0.06)";
                    document.documentElement.appendChild(marker);
                  }
                  const viewportArea = Math.max(1, window.innerWidth) * Math.max(1, window.innerHeight);
                  const rectArea = Math.max(0, rect.width) * Math.max(0, rect.height);
                  const useCrosshair = rectArea > (0.6 * viewportArea);
                  if (useCrosshair) {
                    const cx = rect.left + rect.width / 2;
                    const cy = rect.top + rect.height / 2;
                    const r = 22;
                    marker.style.borderRadius = "999px";
                    marker.style.width = (r * 2) + "px";
                    marker.style.height = (r * 2) + "px";
                    marker.style.left = Math.max(0, cx - r) + "px";
                    marker.style.top = Math.max(0, cy - r) + "px";
                    marker.style.background = "rgba(255, 0, 80, 0.10)";
                  } else {
                    marker.style.borderRadius = "8px";
                    marker.style.left = Math.max(0, rect.left) + "px";
                    marker.style.top = Math.max(0, rect.top) + "px";
                    marker.style.width = Math.max(0, rect.width) + "px";
                    marker.style.height = Math.max(0, rect.height) + "px";
                    marker.style.background = "rgba(255, 0, 80, 0.06)";
                  }
                } catch (e) {}
                """,
                element,
            )
        except Exception:
            pass
        return element, {"positioned": True, "selector": selector_used, "rect": rect or {}}
    except Exception as exc:
        return element, {"positioned": False, "reason": str(exc), "selector": selector_used, "rect": rect or {}}


def capture_page_state(driver):
    """
    Capture a small, safe-to-log snapshot of the current browser page state.

    Intentionally excludes cookie values and storage values.
    """
    cookie_names = []
    try:
        cookies = driver.get_cookies() or []
        cookie_names = sorted({cookie.get("name") for cookie in cookies if cookie.get("name")})
    except Exception:
        cookie_names = []

    local_storage_keys = _safe_execute_script(driver, "try { return Object.keys(localStorage); } catch (e) { return []; }", default=[]) or []
    session_storage_keys = _safe_execute_script(driver, "try { return Object.keys(sessionStorage); } catch (e) { return []; }", default=[]) or []

    return {
        "ready_state": _safe_execute_script(driver, "return document.readyState", default=""),
        "user_agent": _safe_execute_script(driver, "return navigator.userAgent", default=""),
        "viewport": _safe_execute_script(
            driver,
            "return { innerWidth: window.innerWidth, innerHeight: window.innerHeight, devicePixelRatio: window.devicePixelRatio };",
            default={},
        ) or {},
        "cookies": {
            "count": len(cookie_names),
            "names": cookie_names[:60],
        },
        "local_storage": {
            "count": len(local_storage_keys),
            "keys": list(local_storage_keys)[:60],
        },
        "session_storage": {
            "count": len(session_storage_keys),
            "keys": list(session_storage_keys)[:60],
        },
        "next_data_present": bool(_safe_execute_script(driver, "return Boolean(document.getElementById('__NEXT_DATA__'))", default=False)),
        "px": {
            "uuid": _safe_execute_script(driver, "return window._pxUuid || ''", default="") or "",
            "app_id": _safe_execute_script(driver, "return window._pxAppId || ''", default="") or "",
            "vid": _safe_execute_script(driver, "return window._pxVid || ''", default="") or "",
        },
    }


def detect_challenge(driver):
    text = page_text(driver)
    title = safe_title(driver)
    visible_haystack = f"{title}\n{text}"
    visible_matches = [name for name, pattern in VISIBLE_CHALLENGE_PATTERNS if pattern.search(visible_haystack)]
    dom_matches = _dom_source_matches(driver)
    source_matches = set(dom_matches)
    if not source_matches:
        source = page_source_excerpt(driver, max_chars=250000)
        source_matches.update(name for name, pattern in SOURCE_CHALLENGE_PATTERNS if pattern.search(source))
    source_matches = sorted(source_matches)
    matched = sorted(set(visible_matches + source_matches))
    has_usable_listing_content = is_usable_listing_page(visible_haystack)
    # "source matches" are treated as *soft* signals by default, because many
    # pages embed captcha providers or bot managers without actively blocking.
    #
    # We upgrade to a hard challenge when:
    # - Cloudflare challenge platform markers appear, OR
    # - The page looks like a dedicated captcha interstitial (very little text)
    #   and the only strong signals are captcha frames.
    text_len = len((text or "").strip())
    likely_captcha_interstitial = text_len <= 120 and not title
    hard_by_source = (
        "cloudflare_challenge" in source_matches
        # DOM-verified PX challenge UI (not just script instrumentation).
        # _dom_source_matches checks actual element presence (#px-captcha,
        # .px-captcha-container, etc.) which only appear during an active challenge.
        or "px_captcha" in source_matches
        # Realtor's own block page (CSS-animated transparent text, honeypot spans).
        or "realtor_block" in source_matches
        or (
            likely_captcha_interstitial
            and any(name in source_matches for name in ("recaptcha_frame", "hcaptcha_frame"))
        )
    )
    is_hard_challenge = bool(visible_matches or hard_by_source)

    # Captcha frame patterns on a content-rich page with no visible challenge
    # markers are likely embedded widgets in normal page UX (login forms,
    # checkout flows) rather than bot-challenge responses. Exclude them from
    # is_soft_challenge when they are the *only* non-instrumentation signal and
    # the page has substantial text content — this avoids false positives on
    # sites like Walgreens that embed reCAPTCHA in every page for form
    # protection, regardless of whether the visitor is a bot.
    _CAPTCHA_FRAME_PATTERNS = {"recaptcha_frame", "hcaptcha_frame"}
    _EXCLUDED_FROM_SOFT = {"perimeterx_instrumentation"}
    _non_excluded = [m for m in source_matches if m not in _EXCLUDED_FROM_SOFT]
    _substantial_content = text_len > 2500
    _frame_only = bool(_non_excluded) and all(m in _CAPTCHA_FRAME_PATTERNS for m in _non_excluded)
    if _substantial_content and _frame_only and not visible_matches:
        source_matches_for_soft = []
    else:
        source_matches_for_soft = [m for m in source_matches if m not in _EXCLUDED_FROM_SOFT]

    return {
        "is_challenge": is_hard_challenge,
        "is_soft_challenge": bool(source_matches_for_soft and not is_hard_challenge),
        "has_usable_listing_content": has_usable_listing_content,
        "matched_patterns": matched,
        "visible_matched_patterns": sorted(set(visible_matches)),
        "source_matched_patterns": sorted(set(source_matches)),
        "title": title,
        "current_url": safe_url(driver),
        "body_text_excerpt": text[:1200],
    }


def is_usable_listing_page(text):
    return sum(1 for pattern in USABLE_LISTING_PATTERNS if pattern.search(text or "")) >= 2


def safe_title(driver):
    try:
        return driver.title
    except Exception:
        return ""


def safe_url(driver):
    try:
        return driver.current_url
    except Exception:
        return ""


def _diagnostic_example_limit():
    try:
        return max(0, int(os.environ.get("SCRAPER_DIAGNOSTIC_EXAMPLE_LIMIT", "3")))
    except ValueError:
        return 3


def _parse_diagnostic_prefix(prefix):
    parts = str(prefix or "").split("_")
    provider = parts[0] if parts else "unknown"
    zip_code = parts[1] if len(parts) > 1 and parts[1].isdigit() else ""
    reason_start = 2 if zip_code else 1
    reason = "_".join(parts[reason_start:]) or "unknown"
    provider = re.sub(r"[^a-zA-Z0-9_.-]+", "_", provider).strip("_").lower() or "unknown"
    reason = re.sub(r"[^a-zA-Z0-9_.-]+", "_", reason).strip("_").lower() or "unknown"
    return provider, zip_code, reason


def _copy_diagnostic_example(diagnostics_path, safe_prefix, timestamp, metadata_path, html_path, screenshot_path):
    limit = _diagnostic_example_limit()
    if limit <= 0:
        return None

    provider, zip_code, reason = _parse_diagnostic_prefix(safe_prefix)
    examples_root = diagnostics_path / "RecentExamples"
    bucket_dir = examples_root / provider / reason
    bucket_dir.mkdir(parents=True, exist_ok=True)

    copied_files = {}
    for label, source_path in (
        ("metadata", metadata_path),
        ("html", html_path),
        ("screenshot", screenshot_path),
    ):
        if not source_path.exists():
            continue
        target_path = bucket_dir / f"{timestamp}_{safe_prefix}{source_path.suffix}"
        shutil.copy2(source_path, target_path)
        copied_files[label] = str(target_path.relative_to(diagnostics_path))

    example = {
        "timestamp": timestamp,
        "captured_at_epoch": time.time(),
        "provider": provider,
        "zip_code": zip_code,
        "reason": reason,
        "prefix": safe_prefix,
        "files": copied_files,
        "source_files": {
            "metadata": metadata_path.name,
            "html": html_path.name,
            "screenshot": screenshot_path.name,
        },
    }

    manifest_path = examples_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        manifest = {}
    buckets = manifest.setdefault("buckets", {})
    bucket_key = f"{provider}/{reason}"
    bucket = buckets.setdefault(bucket_key, {
        "provider": provider,
        "reason": reason,
        "examples": [],
    })
    bucket["examples"] = [
        item for item in bucket.get("examples", [])
        if item.get("prefix") != safe_prefix or item.get("timestamp") != timestamp
    ]
    bucket["examples"].append(example)
    bucket["examples"].sort(
        key=lambda item: (float(item.get("captured_at_epoch") or 0), item.get("timestamp", ""), item.get("prefix", "")),
        reverse=True,
    )

    retained = bucket["examples"][:limit]
    pruned = bucket["examples"][limit:]
    bucket["examples"] = retained
    bucket["updated_at"] = retained[0]["timestamp"] if retained else timestamp
    bucket["limit"] = limit

    for old in pruned:
        for relative_path in (old.get("files") or {}).values():
            try:
                (diagnostics_path / relative_path).unlink(missing_ok=True)
            except OSError:
                pass

    manifest["updated_at"] = datetime.now().isoformat()
    manifest["limit_per_provider_reason"] = limit
    manifest["total_examples"] = sum(len(item.get("examples", [])) for item in buckets.values())
    tmp_path = manifest_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(manifest_path)
    return example


def save_page_diagnostics(driver, diagnostics_dir, prefix, extra=None):
    diagnostics_path = Path(diagnostics_dir).expanduser().resolve()
    diagnostics_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_prefix = re.sub(r"[^a-zA-Z0-9_.-]+", "_", prefix).strip("_") or "page"
    base_path = diagnostics_path / f"{timestamp}_{safe_prefix}"

    challenge = detect_challenge(driver)
    metadata = {
        **challenge,
        "page_state": capture_page_state(driver),
        "extra": extra or {},
    }

    metadata_path = base_path.with_suffix(".json")
    html_path = base_path.with_suffix(".html")
    screenshot_path = base_path.with_suffix(".png")

    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    html_excerpt = ""
    html_size = 0
    html_sha256 = ""
    html_error = ""
    screenshot_error = ""
    try:
        html = driver.page_source or ""
        try:
            html_size = len(html.encode("utf-8"))
        except Exception:
            html_size = len(html)
        html_excerpt = html[:50_000]
        html_sha256 = hashlib.sha256(html.encode("utf-8", errors="ignore")).hexdigest() if isinstance(html, str) else ""
        html_path.write_text(html, encoding="utf-8")
    except Exception as exc:
        html_error = str(exc)
        metadata["html_error"] = html_error
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    try:
        driver.save_screenshot(str(screenshot_path))
    except Exception as exc:
        screenshot_error = str(exc)
        metadata["screenshot_error"] = screenshot_error
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
    recent_example = _copy_diagnostic_example(
        diagnostics_path,
        safe_prefix,
        timestamp,
        metadata_path,
        html_path,
        screenshot_path,
    )

    return {
        "metadata_path": str(metadata_path),
        "html_path": str(html_path),
        "screenshot_path": str(screenshot_path),
        "recent_example": recent_example,
        "html_size": html_size,
        "html_sha256": html_sha256,
        "html_excerpt": html_excerpt,
        "html_error": html_error or None,
        "screenshot_error": screenshot_error or None,
        "challenge": challenge,
    }


def _bezier_points(x0: float, y0: float, x1: float, y1: float, n: int = 16):
    """Quadratic bezier curve with a random control point for natural-looking mouse paths."""
    mid_x = (x0 + x1) / 2 + random.uniform(-50, 50)
    mid_y = (y0 + y1) / 2 + random.uniform(-30, 30)
    points = []
    for i in range(n):
        t = i / max(1, n - 1)
        inv_t = 1 - t
        px = inv_t ** 2 * x0 + 2 * inv_t * t * mid_x + t ** 2 * x1
        py = inv_t ** 2 * y0 + 2 * inv_t * t * mid_y + t ** 2 * y1
        points.append((px, py))
    return points


def _get_px_iframe_coords_via_cdp(driver):
    """
    Pierce the closed shadow root on #px-captcha via CDP and return the viewport
    rect of its visible iframe — the actual interactive region the PX SDK uses.

    Returns a dict with cx, cy, width, height, left, top, or None on any failure.
    The PX SDK expands #px-captcha's outer height for visual styling while the
    interactive iframe stays at ~52px; using the container center misses the target.
    """
    try:
        doc = driver.execute_cdp_cmd("DOM.getDocument", {"depth": 0})
        root_id = doc["root"]["nodeId"]
        qs = driver.execute_cdp_cmd("DOM.querySelector", {
            "nodeId": root_id, "selector": "#px-captcha",
        })
        host_id = qs.get("nodeId")
        if not host_id:
            return None
        desc = driver.execute_cdp_cmd("DOM.describeNode", {
            "nodeId": host_id, "pierce": True, "depth": 4,
        })
        shadow_roots = desc.get("node", {}).get("shadowRoots", [])
        if not shadow_roots:
            return None

        def _find_visible_iframe(nodes):
            for node in nodes:
                if node.get("nodeName", "").upper() == "IFRAME":
                    raw = node.get("attributes", [])
                    attrs = {raw[i]: raw[i + 1] for i in range(0, len(raw) - 1, 2)}
                    style = attrs.get("style", "")
                    if "display: none" not in style and "display:none" not in style:
                        return node.get("nodeId")
                result = _find_visible_iframe(node.get("children", []))
                if result:
                    return result
            return None

        iframe_id = _find_visible_iframe(shadow_roots[0].get("children", []))
        if not iframe_id:
            return None
        quads = driver.execute_cdp_cmd("DOM.getContentQuads", {"nodeId": iframe_id})
        q = (quads.get("quads") or [[]])[0]
        if len(q) < 8:
            return None
        xs, ys = q[0::2], q[1::2]
        l, t, r, b = min(xs), min(ys), max(xs), max(ys)
        if (r - l) < 4 or (b - t) < 4:
            return None
        return {"cx": (l + r) / 2, "cy": (t + b) / 2,
                "width": r - l, "height": b - t, "left": l, "top": t}
    except Exception:
        return None


def _inject_debug_overlay(driver, container_rect, iframe_rect, press_x, press_y):
    """
    Paint colour-coded viewport overlays showing PX targeting coords.

    Yellow  — #px-captcha container (getBoundingClientRect)
    Green   — CDP-pierced shadow iframe rect (interactive region, if found)
    Red dot — actual press_x, press_y sent via CDP
    """
    try:
        driver.execute_script(
            """
            (function(c, ifr, px, py) {
                var old = document.getElementById('_px_dbg');
                if (old) old.remove();
                var root = document.createElement('div');
                root.id = '_px_dbg';
                root.style.cssText = 'position:fixed;top:0;left:0;width:0;height:0;pointer-events:none;';

                function box(l, t, w, h, color, label) {
                    var d = document.createElement('div');
                    d.style.cssText = 'position:fixed;box-sizing:border-box;'
                        + 'border:2px solid ' + color + ';z-index:2147483647;pointer-events:none;'
                        + 'left:' + l + 'px;top:' + t + 'px;width:' + w + 'px;height:' + h + 'px;';
                    var s = document.createElement('span');
                    s.textContent = label;
                    s.style.cssText = 'position:absolute;top:-17px;left:0;font:10px monospace;'
                        + 'background:' + color + ';color:#fff;padding:1px 4px;white-space:nowrap;';
                    d.appendChild(s);
                    root.appendChild(d);
                }
                function dot(x, y, color, label) {
                    var d = document.createElement('div');
                    d.style.cssText = 'position:fixed;width:12px;height:12px;border-radius:50%;'
                        + 'background:' + color + ';z-index:2147483647;pointer-events:none;'
                        + 'left:' + (x-6) + 'px;top:' + (y-6) + 'px;';
                    var s = document.createElement('span');
                    s.textContent = label;
                    s.style.cssText = 'position:absolute;top:14px;left:-4px;font:10px monospace;'
                        + 'background:' + color + ';color:#fff;padding:1px 4px;white-space:nowrap;';
                    d.appendChild(s);
                    root.appendChild(d);
                }

                if (c)   box(c.left, c.top, c.width, c.height, '#f0c000',
                             'container ' + Math.round(c.width) + 'x' + Math.round(c.height));
                if (ifr) box(ifr.left, ifr.top, ifr.width, ifr.height, '#00cc44',
                             'cdp-iframe ' + Math.round(ifr.width) + 'x' + Math.round(ifr.height));
                dot(px, py, '#ff2040', Math.round(px) + ',' + Math.round(py));
                document.documentElement.appendChild(root);
            })(arguments[0], arguments[1], arguments[2], arguments[3]);
            """,
            container_rect,
            iframe_rect,
            press_x,
            press_y,
        )
    except Exception:
        pass


def _get_px_modal_button_rect(driver):
    """
    Locate the PX challenge button inside a nested iframe and return its viewport rect.

    Handles two variants:
      - #px-captcha-modal: full-page fixed iframe (redirect/block-page variant). The
        iframe is position:fixed top:0 left:0 full-viewport, so inner coords are already
        main-page viewport coords (outer offset = 0).
      - #px-captcha-wrapper: in-page overlay variant (challenge injected as an overlay
        onto the content page without a redirect). The outer wrapper is position:fixed
        full-page, so the same zero-offset rule applies for the nested iframe.

    In both cases the iframe's src is typically about:blank (same-origin) so
    contentDocument is accessible without CORS restrictions.
    """
    try:
        result = driver.execute_script("""
            try {
                // Variant 1: full-page replacement (redirect / block-page).
                var iframe = document.getElementById('px-captcha-modal');
                // Variant 2: in-page overlay — wrapper div containing a nested iframe.
                if (!iframe) {
                    var wrapper = document.getElementById('px-captcha-wrapper');
                    if (wrapper) {
                        iframe = wrapper.querySelector('iframe')
                               || wrapper.getElementsByTagName('iframe')[0]
                               || null;
                    }
                }
                if (!iframe) return null;
                var outerRect = iframe.getBoundingClientRect();
                var doc = iframe.contentDocument
                       || (iframe.contentWindow && iframe.contentWindow.document);
                if (!doc) return null;
                var el = doc.getElementById('px-captcha')
                       || doc.querySelector('.px-captcha-container, [id^="px-captcha-"]');
                if (!el) return null;
                var r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) return null;
                var vp = { w: window.innerWidth, h: window.innerHeight };
                // Add outer iframe offset to convert inner-iframe-relative coords to main
                // page viewport coords. For fixed full-page iframes the offset is 0,0 so
                // this doesn't change anything for the redirect variant.
                return {
                    left: outerRect.left + r.left,
                    top:  outerRect.top  + r.top,
                    cx:   outerRect.left + r.left + r.width  / 2,
                    cy:   outerRect.top  + r.top  + r.height / 2,
                    width: r.width, height: r.height, vp: vp
                };
            } catch(e) { return null; }
        """)
        if result and float(result.get("width") or 0) >= 8:
            return result
        return None
    except Exception:
        return None


def _attempt_auto_press_and_hold(driver, debug=False):
    """
    Attempt to resolve a PerimeterX press-and-hold challenge using CDP mouse events.

    Uses Input.dispatchMouseEvent (not WebDriver ActionChains) to simulate a
    realistic press-and-hold gesture: bezier-curve approach, press, micro-jitter
    during hold, then release. Falls back gracefully on any error.

    Handles two challenge layouts:
      - #px-captcha-modal: full-page fixed iframe; button found inside its document
      - #px-captcha / .px-captcha-container: inline challenge widget

    Pass debug=True to paint colour-coded viewport overlays showing the container
    rect (yellow), CDP iframe rect (green), and actual press point (red dot).

    Returns a dict with keys: attempted, cleared, selector, hold_seconds, reason.
    """
    try:
        from selenium.webdriver.common.by import By
    except Exception:
        return {"attempted": False, "reason": "selenium_unavailable"}

    # --- Path A: full-page modal iframe (#px-captcha-modal) ---
    # The button lives inside the iframe document; access it directly.
    modal_rect = _get_px_modal_button_rect(driver)
    if modal_rect:
        element = None
        selector_used = "#px-captcha-modal"
        rect = modal_rect
    else:
        # --- Path B: inline challenge widget via HOLD_SELECTORS ---
        # Ordered from most-specific to least-specific. Skip elements that cover
        # >70% of the viewport — those are the full-page modal iframe itself
        # accidentally matched by [id^='px-captcha-'], not the interactive button.
        HOLD_SELECTORS = [
            "#px-captcha",
            ".px-captcha-container",
            "#px-captcha-wrapper",
            "[id^='px-captcha-']",
        ]

        element = None
        selector_used = None
        _found_rect_check = None
        for selector in HOLD_SELECTORS:
            try:
                candidate = driver.find_element(By.CSS_SELECTOR, selector)
                rect_check = driver.execute_script(
                    """
                    const el = arguments[0];
                    if (!el || !el.getBoundingClientRect) return null;
                    const r = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const visible = style && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && parseFloat(style.opacity || '1') > 0.01;
                    if (!visible) return null;
                    const vw = window.innerWidth, vh = window.innerHeight;
                    // Large overlay containers (in-page challenge wrappers like #px-captcha-wrapper)
                    // can't be pressed directly. Try to get the inner iframe rect for targeting.
                    if (r.width > vw * 0.7 || r.height > vh * 0.7) {
                        try {
                            const iframe = el.querySelector('iframe')
                                        || el.getElementsByTagName('iframe')[0];
                            if (iframe) {
                                const ir = iframe.getBoundingClientRect();
                                if (ir && ir.width >= 8 && ir.height >= 8) {
                                    return { left: ir.left, top: ir.top,
                                             width: ir.width, height: ir.height,
                                             cx: ir.left + ir.width / 2,
                                             cy: ir.top + ir.height / 2,
                                             vp: { w: vw, h: vh },
                                             _inner_iframe: true };
                                }
                            }
                        } catch (e) {}
                        return null;
                    }
                    return { left: r.left, top: r.top, width: r.width, height: r.height };
                    """,
                    candidate,
                )
                if rect_check and float(rect_check.get("width") or 0) >= 8 and float(rect_check.get("height") or 0) >= 8:
                    element = candidate
                    selector_used = selector
                    if rect_check.get("_inner_iframe"):
                        selector_used = f"{selector}/inner-iframe"
                    _found_rect_check = rect_check
                    break
            except Exception:
                continue

        if element is None:
            return {"attempted": False, "reason": "no_element"}

    if element is not None:
        # Path B only: scroll into view and measure rect.
        # For the inner-iframe case the coords were already captured during element
        # discovery; skip the re-measurement to avoid landing on the large outer container.
        if _found_rect_check and _found_rect_check.get("_inner_iframe"):
            rect = _found_rect_check
        else:
            try:
                driver.execute_script(
                    "try { arguments[0].scrollIntoView({block:'center',inline:'center'}); } catch(e) {}",
                    element,
                )
                time.sleep(random.uniform(0.25, 0.45))
            except Exception:
                pass

            try:
                rect = driver.execute_script(
                    """
                    const el = arguments[0];
                    const r = el.getBoundingClientRect();
                    const vp = { w: window.innerWidth, h: window.innerHeight };
                    return { left: r.left, top: r.top,
                             cx: r.left + r.width / 2, cy: r.top + r.height / 2,
                             width: r.width, height: r.height, vp: vp };
                    """,
                    element,
                ) or {}
            except Exception:
                return {"attempted": False, "reason": "rect_error"}

    vp = rect.get("vp") or {}
    vp_w = float(vp.get("w") or 1024)
    vp_h = float(vp.get("h") or 768)

    # Try CDP shadow DOM piercing to get the exact interactive iframe rect.
    # The PX SDK expands #px-captcha height visually (~150px) while the iframe
    # stays at ~52px; using the container center misses the interactive region.
    iframe_coords = _get_px_iframe_coords_via_cdp(driver)
    if iframe_coords:
        target_x = float(iframe_coords["cx"])
        target_y = float(iframe_coords["cy"])
        w = float(iframe_coords["width"])
        h = float(iframe_coords["height"])
    else:
        target_x = float(rect.get("cx") or 0)
        target_y = float(rect.get("cy") or 0)
        w = float(rect.get("width") or 40)
        h = float(rect.get("height") or 40)
        if h > 80:
            # The PX SDK places the interactive ~52px iframe at the TOP of
            # #px-captcha; the remaining height is a non-interactive alert <p>
            # below it. Targeting the container center (cy) lands at y=50 from
            # the top — right at the iframe's bottom edge. Target y=26 instead
            # (center of the 52px iframe) so the press lands squarely on it.
            container_top = float(rect.get("top") or (target_y - h / 2))
            target_y = container_top + 26.0
            h = 52.0

    # Start position: somewhere in the viewport away from the button
    start_x = vp_w * random.uniform(0.1, 0.35)
    start_y = vp_h * random.uniform(0.25, 0.65)

    move_path = _bezier_points(start_x, start_y, target_x, target_y, n=random.randint(14, 20))

    # Maximum hold before giving up. PX can require up to ~30s; we watch the
    # DOM and release as soon as the challenge element disappears.
    _HOLD_MAX = 30.0
    _PX_STILL_PRESENT_JS = (
        "return Boolean("
        "document.getElementById('px-captcha')"
        "||document.querySelector('.px-captcha-container')"
        "||document.getElementById('px-captcha-modal')"
        "||document.getElementById('px-captcha-wrapper'));"
    )

    hold_seconds = 0.0

    try:
        # Move mouse to the button along a curved path
        for px, py in move_path:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": round(px, 1),
                "y": round(py, 1),
                "button": "none",
                "buttons": 0,
                "clickCount": 0,
                "deltaX": 0,
                "deltaY": 0,
                "modifiers": 0,
                "pointerType": "mouse",
            })
            time.sleep(random.uniform(0.010, 0.028))

        # Land slightly off-center within the button
        press_x = target_x + random.uniform(-w * 0.18, w * 0.18)
        press_y = target_y + random.uniform(-h * 0.12, h * 0.12)

        if debug:
            container_rect_dbg = {
                "left": float(rect.get("left") or 0),
                "top": float(rect.get("top") or 0),
                "width": float(rect.get("width") or 0),
                "height": float(rect.get("height") or 0),
            }
            _inject_debug_overlay(driver, container_rect_dbg, iframe_coords, press_x, press_y)

        # Press down — pressure=0.5 matches real desktop mouse button press semantics
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": round(press_x, 1),
            "y": round(press_y, 1),
            "button": "left",
            "buttons": 1,
            "clickCount": 1,
            "modifiers": 0,
            "pointerType": "mouse",
            "pressure": 0.5,
        })

        # Hold with micro-jitter. Poll DOM every ~0.5s and release as soon as
        # the challenge element disappears — PX hold duration is variable (up to
        # ~30s). Releasing before the page clears causes a failed attempt.
        press_start = time.time()
        hold_deadline = press_start + _HOLD_MAX
        last_check = press_start
        _CHECK_INTERVAL = 0.5

        while time.time() < hold_deadline:
            time.sleep(random.uniform(0.06, 0.14))
            now = time.time()

            # Check whether the challenge element is still in the DOM
            if now - last_check >= _CHECK_INTERVAL:
                last_check = now
                try:
                    if not driver.execute_script(_PX_STILL_PRESENT_JS):
                        break  # challenge cleared — release immediately
                except Exception:
                    pass

            if random.random() < 0.45:
                jx = press_x + random.uniform(-1.8, 1.8)
                jy = press_y + random.uniform(-1.2, 1.2)
                driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                    "type": "mouseMoved",
                    "x": round(jx, 1),
                    "y": round(jy, 1),
                    "button": "left",
                    "buttons": 1,
                    "clickCount": 0,
                    "modifiers": 0,
                    "pointerType": "mouse",
                    "pressure": 0.5,
                })

        hold_seconds = time.time() - press_start

        # Release — pressure returns to 0 (button no longer depressed)
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": round(press_x, 1),
            "y": round(press_y, 1),
            "button": "left",
            "buttons": 0,
            "clickCount": 1,
            "modifiers": 0,
            "pointerType": "mouse",
            "pressure": 0.0,
        })

    except Exception as exc:
        return {
            "attempted": True,
            "cleared": False,
            "reason": f"cdp_error: {exc}",
            "selector": selector_used,
            "hold_seconds": hold_seconds,
        }

    # Give the page a moment to respond before checking
    time.sleep(random.uniform(1.2, 1.8))
    result = detect_challenge(driver)
    cleared = not result.get("is_challenge")
    return {
        "attempted": True,
        "cleared": cleared,
        "selector": selector_used,
        "hold_seconds": hold_seconds,
        "reason": "cleared" if cleared else "still_challenged",
        "challenge": result,
    }


def wait_for_manual_challenge(driver, wait_seconds, poll_seconds=2.0):
    """
    Pause execution to allow a human to resolve a visible verification challenge.

    For PerimeterX press-and-hold challenges, an automated CDP-based interaction
    is attempted first. If that clears the challenge, no human input is needed.
    Otherwise, execution pauses for up to wait_seconds for manual resolution.
    """
    if wait_seconds is None:
        deadline = None
    else:
        deadline = time.time() + max(0.0, float(wait_seconds or 0.0))

    initial = detect_challenge(driver)
    if not initial.get("is_challenge"):
        return initial

    # For PerimeterX press-and-hold, try CDP-based auto-interaction before
    # falling back to human-in-the-loop. This lets debugging runs complete
    # without manual intervention when the challenge can be auto-resolved.
    matched = initial.get("matched_patterns") or []
    if "press_and_hold" in matched or "px_captcha" in matched:
        # Brief wait: _dom_source_matches detects element presence but the PX SDK
        # may not have finished sizing the iframes yet.  Poll until the element
        # has a real rendered height before attempting the hold.
        _PX_SIZED_JS = (
            "return (function(){"
            "if(document.getElementById('px-captcha-modal'))return true;"
            "var wrapper=document.getElementById('px-captcha-wrapper');"
            "if(wrapper){"
            "var wr=wrapper.getBoundingClientRect();"
            "if(wr.width>=50&&wr.height>=30)return true;"
            "}"
            "var el=document.getElementById('px-captcha')"
            "||document.querySelector('.px-captcha-container');"
            "if(!el)return false;"
            "var r=el.getBoundingClientRect();"
            "return r.width>=50&&r.height>=30;"
            "})();"
        )
        _deadline = time.time() + 8.0
        while time.time() < _deadline:
            try:
                if driver.execute_script(_PX_SIZED_JS):
                    break
            except Exception:
                pass
            time.sleep(0.5)

        for _px_attempt in range(1, 3):
            auto = _attempt_auto_press_and_hold(driver, debug=True)
            if auto.get("cleared"):
                print(
                    f"[hil] Press-and-hold auto-resolved on attempt {_px_attempt}/2 via CDP "
                    f"(selector={auto.get('selector')} hold={auto.get('hold_seconds', 0):.1f}s)",
                    flush=True,
                )
                return detect_challenge(driver)
            print(
                f"[hil] Press-and-hold auto-attempt {_px_attempt}/2 did not clear "
                f"(selector={auto.get('selector', '')} reason={auto.get('reason', '')})",
                flush=True,
            )
            if _px_attempt < 2:
                time.sleep(random.uniform(1.5, 2.5))
        print("[hil] Auto-attempts exhausted; falling back to human-in-the-loop.", flush=True)

    _, hil_meta = _position_cursor_for_hil(driver)
    hil_rect = (hil_meta or {}).get("rect") if isinstance(hil_meta, dict) else {}
    hil_cx = hil_rect.get("cx") if isinstance(hil_rect, dict) else None
    hil_cy = hil_rect.get("cy") if isinstance(hil_rect, dict) else None
    hil_center = ""
    if isinstance(hil_cx, (int, float)) and isinstance(hil_cy, (int, float)):
        hil_center = f" center=({round(float(hil_cx), 1)},{round(float(hil_cy), 1)})"


    wait_label = "∞" if deadline is None else str(int(max(0.0, float(wait_seconds or 0.0))))
    print(
        "[hil] Challenge detected; solve it in the visible browser window. "
        f"Waiting up to {wait_label}s. "
        f"hil_prepared={bool(hil_meta and hil_meta.get('positioned'))} "
        f"target={hil_meta.get('selector') if hil_meta else ''} "
        f"{hil_center} "
        f"matched={initial.get('matched_patterns') or []} url={initial.get('current_url') or ''}",
        flush=True,
    )

    last = initial
    last_emit = 0.0
    while deadline is None or time.time() < deadline:
        time.sleep(max(0.2, float(poll_seconds or 2.0)))
        last = detect_challenge(driver)
        if not last.get("is_challenge"):
            return last
        if deadline is not None and (time.time() - last_emit) >= 15:
            remaining = int(max(0.0, deadline - time.time()))
            print(
                f"[hil] Still challenged; {remaining}s remaining. matched={last.get('matched_patterns') or []}",
                flush=True,
            )
            last_emit = time.time()
    return last
