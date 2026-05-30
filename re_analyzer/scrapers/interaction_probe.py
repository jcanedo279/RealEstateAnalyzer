"""
Interaction quality probe for automated press-and-hold gestures.

Loads a synthetic #px-captcha test page into the browser, runs
_attempt_auto_press_and_hold against it, then analyzes the captured events
for bot-detectable anti-patterns.

Anti-patterns checked (with severity):
  critical  isTrusted=false on any event (JS-dispatch, not CDP/OS-level)
  high      missing pointer events (pointerdown/pointerup)
  high      hold duration outside human range
  medium    zero pointer pressure throughout interaction
  medium    no mouse movement before pressing
  medium    missing mouseenter/mouseover before mousedown
  medium    approach movement velocity too uniform (low coefficient of variation)
  low       no micro-movement during hold
"""

import argparse
import json
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Test harness HTML
# ---------------------------------------------------------------------------

_INTERACTION_TEST_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Interaction Probe</title>
<style>
  body {
    background: #e8eaf0; margin: 0; padding: 0;
    font-family: -apple-system, sans-serif;
  }
  #px-captcha {
    position: fixed; left: 50%; top: 50%;
    transform: translate(-50%, -50%);
    width: 280px; height: 80px;
    background: linear-gradient(135deg, #1a2f6b 0%, #2756c5 100%);
    color: #fff; font-size: 15px; font-weight: 700; letter-spacing: 0.04em;
    border-radius: 10px; cursor: pointer; user-select: none;
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 4px 18px rgba(0,0,0,0.28);
    transition: background 0.15s;
  }
  #px-captcha:active {
    background: linear-gradient(135deg, #2756c5 0%, #1a2f6b 100%);
  }
</style>
</head>
<body>
<div id="px-captcha">PRESS &amp; HOLD</div>
<script>
(function () {
  'use strict';

  var events = [];
  var EVT_TYPES = [
    'pointerenter', 'pointerover', 'pointermove', 'pointerdown', 'gotpointercapture',
    'pointerup', 'lostpointercapture', 'pointerout', 'pointerleave',
    'mouseenter', 'mouseover', 'mousemove', 'mousedown', 'mouseup', 'click', 'mouseleave'
  ];

  function captureEvent(e) {
    var isPE = (typeof PointerEvent !== 'undefined') && (e instanceof PointerEvent);
    events.push({
      type:        e.type,
      isTrusted:   e.isTrusted,
      timeStamp:   e.timeStamp,
      button:      e.button != null ? e.button : null,
      buttons:     e.buttons != null ? e.buttons : null,
      clientX:     e.clientX || 0,
      clientY:     e.clientY || 0,
      movementX:   e.movementX || 0,
      movementY:   e.movementY || 0,
      pressure:    isPE ? e.pressure : null,
      tiltX:       isPE ? e.tiltX : null,
      tiltY:       isPE ? e.tiltY : null,
      pointerType: isPE ? e.pointerType : null,
      pointerId:   isPE ? e.pointerId : null,
      isPrimary:   isPE ? e.isPrimary : null
    });
  }

  for (var i = 0; i < EVT_TYPES.length; i++) {
    document.addEventListener(EVT_TYPES[i], captureEvent, { capture: true, passive: true });
  }

  window.__interactionEvents = events;
  window.__pageReady = true;
}());
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Anti-pattern analysis
# ---------------------------------------------------------------------------

def _velocity_profile(move_events: list) -> dict:
    """Compute move-to-move speed stats to detect unnaturally uniform movement."""
    if len(move_events) < 3:
        return {"n": len(move_events), "mean_px_per_ms": None, "std_px_per_ms": None, "cv": None}
    speeds = []
    for i in range(1, len(move_events)):
        dt = move_events[i]["timeStamp"] - move_events[i - 1]["timeStamp"]
        dx = move_events[i]["clientX"] - move_events[i - 1]["clientX"]
        dy = move_events[i]["clientY"] - move_events[i - 1]["clientY"]
        dist = (dx ** 2 + dy ** 2) ** 0.5
        if dt > 0:
            speeds.append(dist / dt)
    if not speeds:
        return {"n": len(move_events), "mean_px_per_ms": None, "std_px_per_ms": None, "cv": None}
    mean = sum(speeds) / len(speeds)
    variance = sum((s - mean) ** 2 for s in speeds) / len(speeds)
    std = variance ** 0.5
    cv = std / mean if mean > 0 else None
    return {
        "n": len(move_events),
        "mean_px_per_ms": round(mean, 4),
        "std_px_per_ms": round(std, 4),
        "cv": round(cv, 4) if cv is not None else None,
    }


def analyze_interaction_antipatterns(events: list) -> dict:
    """
    Analyze captured browser events for bot-detectable anti-patterns.

    Returns a dict with:
      antipatterns  — list of {id, severity, message}
      summary       — quantitative stats derived from the event stream
      counts        — {critical, high, medium, low}
    """
    antipatterns = []

    if not events:
        antipatterns.append({
            "id": "no_events",
            "severity": "critical",
            "message": "No events captured — interaction did not reach the element.",
        })
        return {
            "antipatterns": antipatterns,
            "antipattern_count": 1,
            "counts": {"critical": 1, "high": 0, "medium": 0, "low": 0},
            "summary": {},
        }

    # isTrusted ------------------------------------------------------------
    untrusted = [e for e in events if e.get("isTrusted") is False]
    if untrusted:
        antipatterns.append({
            "id": "untrusted_events",
            "severity": "critical",
            "message": (
                f"{len(untrusted)} event(s) have isTrusted=false — "
                f"types: {[e['type'] for e in untrusted[:6]]}. "
                "Events dispatched via JS dispatchEvent() are not trusted; "
                "CDP Input.dispatchMouseEvent should produce isTrusted=true."
            ),
        })

    # Pointer events -------------------------------------------------------
    has_pointerdown = any(e["type"] == "pointerdown" for e in events)
    has_pointerup = any(e["type"] == "pointerup" for e in events)
    has_mousedown = any(e["type"] == "mousedown" for e in events)
    has_mouseup = any(e["type"] == "mouseup" for e in events)

    if not has_pointerdown:
        antipatterns.append({
            "id": "missing_pointerdown",
            "severity": "high",
            "message": (
                "No pointerdown event fired. CDP Input.dispatchMouseEvent on this "
                "browser version does not auto-generate PointerEvents. PerimeterX "
                "press-and-hold listens for pointermove/pointerdown; consider also "
                "sending Input.dispatchTouchEvent or pointer-type events."
            ),
        })
    if not has_pointerup:
        antipatterns.append({
            "id": "missing_pointerup",
            "severity": "high",
            "message": "No pointerup event fired.",
        })

    # Pointer pressure -----------------------------------------------------
    pd_evt = next((e for e in events if e["type"] == "pointerdown"), None)
    if pd_evt is not None:
        pressure = pd_evt.get("pressure")
        if pressure is not None and float(pressure) == 0.0:
            antipatterns.append({
                "id": "zero_pressure",
                "severity": "medium",
                "message": (
                    "pointerdown pressure=0. Real mouse presses report pressure=0.5; "
                    "touch events report 0–1 based on force. "
                    "Add 'pressure' to CDP pointer event dispatch."
                ),
            })

    # Approach movement ----------------------------------------------------
    md_evt = next((e for e in events if e["type"] == "mousedown"), None)
    mu_evt = next((e for e in events if e["type"] == "mouseup"), None)

    moves_before_down = []
    if md_evt:
        moves_before_down = [
            e for e in events
            if e["type"] == "mousemove" and e["timeStamp"] < md_evt["timeStamp"]
        ]
    if not moves_before_down:
        antipatterns.append({
            "id": "no_approach_movement",
            "severity": "medium",
            "message": (
                "No mousemove events before mousedown. "
                "Real users always approach via continuous cursor movement."
            ),
        })

    # Mouseenter / mouseover before press ----------------------------------
    has_mouseenter = any(e["type"] == "mouseenter" for e in events)
    has_mouseover = any(e["type"] == "mouseover" for e in events)
    if md_evt and not has_mouseenter and not has_mouseover:
        antipatterns.append({
            "id": "missing_approach_events",
            "severity": "medium",
            "message": (
                "mousedown fired without a prior mouseenter or mouseover. "
                "The element was not entered via natural cursor movement."
            ),
        })

    # Movement during hold -------------------------------------------------
    moves_during_hold = []
    if md_evt and mu_evt:
        moves_during_hold = [
            e for e in events
            if e["type"] == "mousemove"
            and e["timeStamp"] > md_evt["timeStamp"]
            and e["timeStamp"] < mu_evt["timeStamp"]
        ]
    if has_mousedown and has_mouseup and not moves_during_hold:
        antipatterns.append({
            "id": "no_hold_microjitter",
            "severity": "low",
            "message": (
                "No mousemove during the hold window. "
                "Human hands produce micro-jitter (1–3 px) while pressing."
            ),
        })

    # Hold duration --------------------------------------------------------
    hold_ms = None
    if md_evt and mu_evt:
        hold_ms = mu_evt["timeStamp"] - md_evt["timeStamp"]
        if hold_ms < 1500:
            antipatterns.append({
                "id": "hold_too_short",
                "severity": "high",
                "message": (
                    f"Hold duration {hold_ms:.0f} ms is below the human minimum (~1500 ms). "
                    "PerimeterX requires sustained contact."
                ),
            })
        elif hold_ms > 6000:
            antipatterns.append({
                "id": "hold_too_long",
                "severity": "low",
                "message": (
                    f"Hold duration {hold_ms:.0f} ms exceeds the typical human range (~5000 ms)."
                ),
            })

    # Velocity uniformity --------------------------------------------------
    approach_velocity = _velocity_profile(moves_before_down)
    hold_velocity = _velocity_profile(moves_during_hold)

    if (
        approach_velocity.get("cv") is not None
        and approach_velocity["cv"] < 0.15
        and len(moves_before_down) >= 5
    ):
        antipatterns.append({
            "id": "approach_uniform_velocity",
            "severity": "medium",
            "message": (
                f"Approach movement has very uniform speed "
                f"(CV={approach_velocity['cv']:.3f}, threshold=0.15). "
                "Real cursor paths show significant velocity variation."
            ),
        })

    # Pressure stats across all pointer events during hold ------------------
    pressure_values = [
        float(e["pressure"])
        for e in events
        if e.get("pressure") is not None and e["type"] in ("pointerdown", "pointermove", "pointerup")
    ]
    hold_pressure_values = [
        float(e["pressure"])
        for e in events
        if e.get("pressure") is not None
        and e["type"] == "pointermove"
        and md_evt is not None
        and mu_evt is not None
        and e["timeStamp"] > md_evt["timeStamp"]
        and e["timeStamp"] < mu_evt["timeStamp"]
    ]
    pressure_stats: dict = {}
    if pressure_values:
        pressure_stats["min"] = round(min(pressure_values), 3)
        pressure_stats["max"] = round(max(pressure_values), 3)
        pressure_stats["mean"] = round(sum(pressure_values) / len(pressure_values), 3)
        pressure_stats["all_zero"] = all(v == 0.0 for v in pressure_values)
        pressure_stats["hold_mean"] = (
            round(sum(hold_pressure_values) / len(hold_pressure_values), 3)
            if hold_pressure_values else None
        )

    # Approach path length (total pixel displacement from start to element) -
    approach_path_px: float | None = None
    if len(moves_before_down) >= 2:
        total_dist = sum(
            ((moves_before_down[i]["clientX"] - moves_before_down[i - 1]["clientX"]) ** 2
             + (moves_before_down[i]["clientY"] - moves_before_down[i - 1]["clientY"]) ** 2) ** 0.5
            for i in range(1, len(moves_before_down))
        )
        approach_path_px = round(total_dist, 1)

    # Dominant event source: CDP events should all be isTrusted=true --------
    trusted_count = sum(1 for e in events if e.get("isTrusted") is True)
    untrusted_count = len(untrusted)
    dominant_source = (
        "trusted" if trusted_count > 0 and untrusted_count == 0
        else "mixed" if trusted_count > 0
        else "untrusted" if untrusted_count > 0
        else "unknown"
    )

    # Summary --------------------------------------------------------------
    event_types_seen = sorted({e["type"] for e in events})
    summary = {
        "event_count": len(events),
        "event_types_seen": event_types_seen,
        "has_pointerdown": has_pointerdown,
        "has_pointerup": has_pointerup,
        "has_mousedown": has_mousedown,
        "has_mouseup": has_mouseup,
        "has_mouseenter": has_mouseenter,
        "has_mouseover": has_mouseover,
        "moves_before_down": len(moves_before_down),
        "moves_during_hold": len(moves_during_hold),
        "hold_duration_ms": round(hold_ms, 1) if hold_ms is not None else None,
        "pointerdown_pressure": (
            pd_evt.get("pressure") if pd_evt is not None else None
        ),
        "pressure_stats": pressure_stats or None,
        "approach_path_px": approach_path_px,
        "dominant_event_source": dominant_source,
        "untrusted_event_count": untrusted_count,
        "approach_velocity": approach_velocity,
        "hold_velocity": hold_velocity,
    }

    counts = {
        "critical": sum(1 for a in antipatterns if a["severity"] == "critical"),
        "high":     sum(1 for a in antipatterns if a["severity"] == "high"),
        "medium":   sum(1 for a in antipatterns if a["severity"] == "medium"),
        "low":      sum(1 for a in antipatterns if a["severity"] == "low"),
    }
    return {
        "antipatterns": antipatterns,
        "antipattern_count": len(antipatterns),
        "counts": counts,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Probe runner
# ---------------------------------------------------------------------------

def run_interaction_probe(driver) -> dict:
    """
    Load the synthetic test page into *driver*, fire _attempt_auto_press_and_hold,
    read back the captured event stream, and return an anti-pattern analysis.
    """
    from re_analyzer.scrapers.page_diagnostics import _attempt_auto_press_and_hold

    # Inject the test page via document.write so data: URL CSP restrictions
    # and encoding edge cases don't interfere.
    try:
        driver.get("about:blank")
        driver.execute_script(
            "document.open(); document.write(arguments[0]); document.close();",
            _INTERACTION_TEST_HTML,
        )
    except Exception as exc:
        return {
            "error": f"Failed to load test page: {exc}",
            "antipatterns": [],
            "antipattern_count": 0,
            "counts": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "summary": {},
        }

    # Wait for page ready signal
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            if driver.execute_script("return Boolean(window.__pageReady);"):
                break
        except Exception:
            pass
        time.sleep(0.15)

    time.sleep(0.4)  # Let layout/paint settle before we look for the element

    print("[interaction_probe] test page loaded; running press-and-hold", flush=True)
    interaction_result = _attempt_auto_press_and_hold(driver)
    print(
        f"[interaction_probe] interaction done: "
        f"attempted={interaction_result.get('attempted')} "
        f"cleared={interaction_result.get('cleared')} "
        f"selector={interaction_result.get('selector')} "
        f"hold={interaction_result.get('hold_seconds', 0):.1f}s",
        flush=True,
    )

    # Give the page a moment to receive all trailing events
    time.sleep(1.0)

    try:
        events = driver.execute_script("return window.__interactionEvents || [];") or []
    except Exception as exc:
        events = []
        print(f"[interaction_probe] could not read events: {exc}", flush=True)

    print(f"[interaction_probe] {len(events)} events captured", flush=True)

    analysis = analyze_interaction_antipatterns(events)

    counts = analysis.get("counts") or {}
    antipatterns = analysis.get("antipatterns") or []
    print(
        f"[interaction_probe] antipatterns: "
        f"critical={counts.get('critical', 0)} "
        f"high={counts.get('high', 0)} "
        f"medium={counts.get('medium', 0)} "
        f"low={counts.get('low', 0)}",
        flush=True,
    )
    for ap in antipatterns:
        print(
            f"[interaction_probe]   [{ap['severity'].upper():8s}] {ap['id']}: {ap['message'][:120]}",
            flush=True,
        )

    return {
        "interaction_result": interaction_result,
        "events_captured": len(events),
        **analysis,
    }


def run_interaction_probe_standalone(
    headless: bool = False,
    chrome_path: str = "",
    chromedriver_path: str = "",
) -> dict:
    """
    Set up a fresh driver, run the interaction probe, and return the result.
    Intended for CLI invocation and standalone testing.
    """
    from re_analyzer.scrapers.scraping_utility import (
        CHROME_BINARY_EXECUTABLE_PATH,
        CHROMEDRIVER_EXECUTABLE_PATH,
        DriverConfig,
        get_selenium_driver,
    )

    driver_config = DriverConfig(
        browser_executable_path=chrome_path or CHROME_BINARY_EXECUTABLE_PATH,
        chromedriver_executable_path=chromedriver_path or CHROMEDRIVER_EXECUTABLE_PATH,
        headless=headless,
        ignore_detection=False,
        random_profile=False,
        clean_profile=False,
        manual_challenge_wait_seconds=0,
    )

    with get_selenium_driver("about:blank", driver_config=driver_config) as driver:
        return run_interaction_probe(driver)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description="Probe automated press-and-hold interaction quality for bot-detection anti-patterns."
    )
    parser.add_argument("--no-headless", action="store_true", help="Show the browser window.")
    parser.add_argument("--chrome-path", default="", help="Chrome binary path override.")
    parser.add_argument("--chromedriver-path", default="", help="Chromedriver path override.")
    parser.add_argument(
        "--output-dir", default="",
        help="Directory to write INTERACTION_PROBE_SUMMARY JSON. Defaults to stdout only.",
    )
    return parser.parse_args()


def main():
    from re_analyzer.utility.utility import DATA_PATH
    from datetime import datetime, timezone

    args = parse_args()
    started_at = datetime.now(timezone.utc)

    result = run_interaction_probe_standalone(
        headless=not args.no_headless,
        chrome_path=str(args.chrome_path or "").strip(),
        chromedriver_path=str(args.chromedriver_path or "").strip(),
    )

    completed_at = datetime.now(timezone.utc)
    summary = {
        "kind": "interaction_probe",
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": round((completed_at - started_at).total_seconds(), 2),
        "headless": not args.no_headless,
        **result,
    }

    output_dir = str(args.output_dir or "").strip()
    if output_dir:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        ts = started_at.strftime("%Y%m%d_%H%M%S")
        out_file = out_path / f"{ts}_interaction_probe.json"
        out_file.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        print(f"[interaction_probe] saved to {out_file}", flush=True)

    print("INTERACTION_PROBE_SUMMARY")
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
