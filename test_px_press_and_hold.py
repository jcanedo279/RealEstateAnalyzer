#!/usr/bin/env python3
"""
Local press-and-hold diagnostic.

Opens zillow_captcha_example.html in a Selenium driver and runs:
  1. detect_challenge  — validates DOM detection
  2. element rect      — confirms coordinate targeting is sensible
  3. _attempt_auto_press_and_hold — fires the CDP mouse sequence
  4. window._testResults  — reads back what events the page received

Run from the analyzer_package/ directory:
    cd analyzer_package
    python test_px_press_and_hold.py
"""
import json
import os
import sys
import time

# Make re_analyzer importable when running directly from analyzer_package/
sys.path.insert(0, os.path.dirname(__file__))

from re_analyzer.scrapers.scraping_utility import get_selenium_driver
from re_analyzer.scrapers.page_diagnostics import detect_challenge, _attempt_auto_press_and_hold

HTML_FILE = os.path.join(os.path.dirname(__file__), "zillow_captcha_example.html")
FILE_URL  = f"file://{os.path.abspath(HTML_FILE)}"

SEP = "─" * 60


def _j(obj):
    return json.dumps(obj, indent=2)


def run():
    print(f"\n{SEP}")
    print(f"  PX press-and-hold local diagnostic")
    print(f"  HTML: {FILE_URL}")
    print(SEP)

    with get_selenium_driver("about:blank", ignore_detection=True) as driver:

        print("\n[1/4] Loading page…")
        driver.get(FILE_URL)
        time.sleep(1.2)   # let layout paint

        # ------------------------------------------------------------------
        # Step 1 — detection
        # ------------------------------------------------------------------
        print(f"\n{SEP}\n[2/4] detect_challenge\n{SEP}")
        ch = detect_challenge(driver)
        print(_j({
            "is_challenge":          ch["is_challenge"],
            "is_soft_challenge":     ch["is_soft_challenge"],
            "matched_patterns":      ch["matched_patterns"],
            "visible_matched":       ch["visible_matched_patterns"],
            "source_matched":        ch["source_matched_patterns"],
            "title":                 ch["title"],
        }))

        ok_detect = ch.get("is_challenge")
        if not ok_detect:
            print("\n[FAIL] detect_challenge did not flag this page as a challenge.")
            print("       Check that #px-captcha-wrapper and the 'Press & Hold' text are present.")
        else:
            print("\n[PASS] Challenge detected correctly.")

        # ------------------------------------------------------------------
        # Step 2 — element rect
        # ------------------------------------------------------------------
        print(f"\n{SEP}\n[3/4] #px-captcha element rect\n{SEP}")
        rect = driver.execute_script("""
          try {
            var el = document.getElementById('px-captcha');
            if (!el) return { found: false, reason: 'no_element' };
            var r = el.getBoundingClientRect();
            var style = window.getComputedStyle(el);
            return {
              found:    true,
              left:     r.left,   top:    r.top,
              width:    r.width,  height: r.height,
              cx:       r.left + r.width  / 2,
              cy:       r.top  + r.height / 2,
              display:  style.display,
              viewport: { w: window.innerWidth, h: window.innerHeight },
            };
          } catch(e) { return { found: false, reason: String(e) }; }
        """)
        print(_j(rect))

        ok_rect = (
            rect.get("found")
            and float(rect.get("width")  or 0) >= 8
            and float(rect.get("height") or 0) >= 8
        )
        if not ok_rect:
            print("\n[FAIL] #px-captcha missing or too small. Cannot target the button.")
        else:
            print(f"\n[PASS] Button found at ({rect['cx']:.0f}, {rect['cy']:.0f}) "
                  f"size {rect['width']:.0f}×{rect['height']:.0f}px")

        # ------------------------------------------------------------------
        # Step 3 — press-and-hold attempt
        # ------------------------------------------------------------------
        print(f"\n{SEP}\n[4/4] _attempt_auto_press_and_hold\n{SEP}")
        result = _attempt_auto_press_and_hold(driver, debug=True)
        print(_j({k: v for k, v in result.items() if k != "challenge"}))

        # ------------------------------------------------------------------
        # Step 4 — read back page-side event log
        # ------------------------------------------------------------------
        time.sleep(0.4)
        tr = driver.execute_script("return window._testResults || null;")
        if tr:
            print(f"\n{SEP}\n  Page-side event log (window._testResults)\n{SEP}")
            print(_j(tr))

            hold_ms     = tr.get("hold_duration_ms") or 0
            events      = tr.get("events_received") or []
            approach    = tr.get("approach_moves") or 0
            jitter_n    = tr.get("jitter_moves") or 0
            jitter_max  = tr.get("max_jitter_px") or 0
            coords      = tr.get("pointerdown_coords") or {}

            print(f"\n{SEP}")
            print("  SUMMARY")
            print(SEP)

            _chk("events received",
                 events,
                 "down" in events and "up" in events,
                 "both 'down' and 'up' seen")

            _chk("approach moves",
                 approach,
                 approach > 5,
                 "> 5 move events before press")

            _chk("press coordinates",
                 coords,
                 bool(coords),
                 "pointerdown landed on element")

            _chk("hold duration",
                 f"{hold_ms} ms",
                 hold_ms >= 3000,
                 ">= 3000 ms  (PX threshold)")

            _chk("jitter moves during hold",
                 jitter_n,
                 jitter_n > 0,
                 "> 0  (micro-tremor present)")

            _chk("max jitter distance",
                 f"{jitter_max} px",
                 jitter_max <= 5.0,
                 "<= 5 px  (stays inside button)")

            all_ok = (
                ok_detect and ok_rect
                and "down" in events and "up" in events
                and hold_ms >= 3000
            )
            print(SEP)
            print(f"  Overall: {'ALL CHECKS PASS ✓' if all_ok else 'ISSUES FOUND — see above'}")
            print(SEP)
        else:
            print("\n[WARN] window._testResults not available.")
            print("       Make sure the HTML is up to date (zillow_captcha_example.html).")

        input("\n  Press Enter to close the browser…\n")


def _chk(label, value, ok, expect):
    icon = "✓" if ok else "✗"
    print(f"  {icon}  {label:30s}  {str(value)!s:20s}  (expect {expect})")


if __name__ == "__main__":
    run()
