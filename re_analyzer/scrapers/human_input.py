"""
human_input.py

Physics-based human-like mouse movement and scroll simulation via CDP.

Two path-generation algorithms are provided:

  WindMouse  — classic physics model with gravity (pull toward target) and
               wind (random lateral perturbation that fades near the target).
               Produces the organic, slightly curved paths a human hand makes
               when sliding across a desk.

  Bézier     — cubic Bézier curve with ease-in-out speed profile and optional
               micro-overshoot + correction.  Good for precise element targeting.

HumanMouse wraps both into a CDP controller with move(), click(), and scroll():

  mouse = HumanMouse(driver)
  mouse.move(640, 360)
  mouse.click(200, 400)
  mouse.scroll(distance=600, direction="down", style="reading")
"""
from __future__ import annotations

import math
import random
import time
from typing import Iterator, List, Optional, Tuple

Point = Tuple[float, float]


# ─── WindMouse path generator ─────────────────────────────────────────────────

def windmouse_path(
    start: Point,
    end: Point,
    *,
    gravity: float = 9.0,
    wind: float = 3.0,
    max_step: float = 12.0,
    target_area: float = 12.0,
) -> Iterator[Point]:
    """
    Yield (x, y) integer waypoints following the WindMouse physics model.

    Two forces act on the virtual cursor each tick:
      Gravity — constant acceleration toward the destination.
      Wind    — random lateral impulse that diminishes near the target area.

    The cursor naturally decelerates inside `target_area` pixels of the
    destination, landing precisely like a human hand.

    Parameters
    ----------
    gravity     High → straighter path.   Low → loopy arcs.
    wind        High → wobbly curves.     Low → nearly straight.
    max_step    Maximum speed (pixels per physics tick).
    target_area Radius (px) at which to start braking.
    """
    x, y = float(start[0]), float(start[1])
    tx, ty = float(end[0]), float(end[1])
    vx = vy = wx = wy = 0.0
    current_max = float(max_step)

    while True:
        dx, dy = tx - x, ty - y
        dist = math.hypot(dx, dy)
        if dist < 1.0:
            break

        # Wind magnitude fades as we approach the target
        w_mag = min(wind, dist)

        if dist >= target_area:
            wx = wx / math.sqrt(3.0) + (2.0 * random.random() - 1.0) * w_mag / math.sqrt(5.0)
            wy = wy / math.sqrt(3.0) + (2.0 * random.random() - 1.0) * w_mag / math.sqrt(5.0)
        else:
            wx /= math.sqrt(3.0)
            wy /= math.sqrt(3.0)
            if current_max < 3.0:
                current_max = random.random() * 3.0 + 3.0
            else:
                current_max /= math.sqrt(5.0)

        vx += wx + gravity * dx / dist
        vy += wy + gravity * dy / dist

        v_mag = math.hypot(vx, vy)
        if v_mag > current_max:
            rand_scale = current_max / 2.0 + random.random() * current_max / 2.0
            vx = vx / v_mag * rand_scale
            vy = vy / v_mag * rand_scale

        x += vx
        y += vy

        yield (round(x), round(y))

    # Guarantee exact landing
    yield (round(tx), round(ty))


# ─── Bézier path generator ───────────────────────────────────────────────────

def bezier_path(
    start: Point,
    end: Point,
    *,
    n_points: int = 60,
    curve_factor: float = 0.35,
    overshoot_factor: float = 0.02,
) -> List[Point]:
    """
    Return waypoints along a cubic Bézier curve from start to end.

    The two interior control points are placed off the direct axis on a
    randomly chosen side, which creates the natural arc produced when the
    human wrist pivots.  An ease-in-out t-mapping accelerates the cursor
    from rest and decelerates it into the target (log-normal-like profile).

    A small optional overshoot extends the path slightly past the target
    then corrects back — mimicking the micro-overshoot humans produce on
    faster movements.

    Parameters
    ----------
    n_points        Number of waypoints (more = smoother path).
    curve_factor    Arc amplitude as a fraction of total distance.
    overshoot_factor  Overshoot distance as a fraction of total distance.
                    Set to 0 to disable.
    """
    x1, y1 = float(start[0]), float(start[1])
    x2, y2 = float(end[0]), float(end[1])
    dx, dy = x2 - x1, y2 - y1
    dist = math.hypot(dx, dy)

    if dist < 1.0:
        return [(round(x2), round(y2))]

    # Perpendicular unit vector for arc deflection
    px, py = -dy / dist, dx / dist
    side = 1.0 if random.random() > 0.5 else -1.0
    offset = side * random.uniform(0.15, curve_factor) * dist

    cp1x = x1 + dx * 0.30 + px * offset * 0.80
    cp1y = y1 + dy * 0.30 + py * offset * 0.80
    cp2x = x1 + dx * 0.70 + px * offset * 0.40
    cp2y = y1 + dy * 0.70 + py * offset * 0.40

    def _ease(t: float) -> float:
        return t * t * (3.0 - 2.0 * t)

    def _cubic(t: float) -> Tuple[float, float]:
        u = 1.0 - t
        bx = u**3 * x1 + 3 * u**2 * t * cp1x + 3 * u * t**2 * cp2x + t**3 * x2
        by = u**3 * y1 + 3 * u**2 * t * cp1y + 3 * u * t**2 * cp2y + t**3 * y2
        return bx, by

    points: List[Point] = []
    for i in range(n_points + 1):
        bx, by = _cubic(_ease(i / n_points))
        points.append((round(bx), round(by)))

    # Micro-overshoot + correction
    if overshoot_factor > 0 and dist > 30:
        over_dist = overshoot_factor * random.uniform(0.6, 1.4) * dist
        over_x = round(x2 + (x2 - cp2x) / dist * over_dist)
        over_y = round(y2 + (y2 - cp2y) / dist * over_dist)
        steps = max(4, int(over_dist / 3))
        lx, ly = points[-1]
        for i in range(1, steps + 1):
            t = i / steps
            points.append((round(lx + (over_x - lx) * t), round(ly + (over_y - ly) * t)))
        # Drift back to target
        for i in range(1, steps + 1):
            t = _ease(i / steps)
            points.append((round(over_x + (x2 - over_x) * t), round(over_y + (y2 - over_y) * t)))

    points.append((round(x2), round(y2)))
    return points


# ─── HumanMouse ──────────────────────────────────────────────────────────────

class HumanMouse:
    """
    CDP-based human-like mouse and scroll controller.

    All coordinates are in CSS pixels (same space as CDP Input events).
    Instantiate once per page, or call refresh_viewport() after navigation
    to re-read the current window dimensions.

    Example
    -------
    mouse = HumanMouse(driver)
    mouse.move(640, 360)                       # wind-mouse movement
    mouse.click(200, 400)                      # hover + press + release
    mouse.scroll(distance=500, direction="down", style="reading")
    """

    def __init__(
        self,
        driver,
        viewport_width: int = 1280,
        viewport_height: int = 720,
    ) -> None:
        self._driver = driver
        self.W = viewport_width
        self.H = viewport_height
        self.x = viewport_width // 2
        self.y = viewport_height // 3
        self.refresh_viewport()

    # ── Viewport ──────────────────────────────────────────────────────────────

    def refresh_viewport(self) -> None:
        """Re-read window.innerWidth / innerHeight from the live page."""
        try:
            win = self._driver.execute_script(
                "return {w: window.innerWidth, h: window.innerHeight};"
            ) or {}
            self.W = int(win.get("w") or self.W)
            self.H = int(win.get("h") or self.H)
        except Exception:
            pass

    def _clamp(self, x: float, y: float) -> Tuple[int, int]:
        return (
            max(4, min(self.W - 4, round(x))),
            max(4, min(self.H - 4, round(y))),
        )

    # ── Low-level CDP dispatchers ─────────────────────────────────────────────

    def _move(self, x: float, y: float) -> None:
        cx, cy = self._clamp(x, y)
        try:
            self._driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": float(cx),
                "y": float(cy),
                "modifiers": 0,
                "pointerType": "mouse",
            })
            self.x, self.y = cx, cy
        except Exception:
            pass

    def _wheel(self, x: int, y: int, delta_x: float, delta_y: float) -> None:
        """Dispatch a raw WheelEvent.  deltaY > 0 = scroll down."""
        try:
            self._driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseWheel",
                "x": float(x),
                "y": float(y),
                "deltaX": float(delta_x),
                "deltaY": float(delta_y),
                "modifiers": 0,
                "pointerType": "mouse",
            })
        except Exception:
            pass

    def _button(self, event_type: str, button: str = "left") -> None:
        try:
            self._driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": event_type,
                "x": float(self.x),
                "y": float(self.y),
                "button": button,
                "clickCount": 1,
                "modifiers": 0,
                "pointerType": "mouse",
            })
        except Exception:
            pass

    # ── Public API ────────────────────────────────────────────────────────────

    def move(
        self,
        tx: float,
        ty: float,
        *,
        method: str = "wind",
        step_delay_range: Tuple[float, float] = (0.006, 0.022),
    ) -> None:
        """
        Move mouse to (tx, ty) using WindMouse (default) or Bézier path.

        method            'wind' | 'bezier'
        step_delay_range  (min_s, max_s) sleep between each waypoint.
        """
        tx_c, ty_c = self._clamp(tx, ty)
        lo, hi = step_delay_range

        if method == "bezier":
            waypoints: List[Point] = bezier_path((self.x, self.y), (tx_c, ty_c))
        else:
            waypoints = list(windmouse_path((self.x, self.y), (tx_c, ty_c)))

        for wx, wy in waypoints:
            self._move(wx, wy)
            time.sleep(random.uniform(lo, hi))

        self.x, self.y = tx_c, ty_c

    def hover(
        self,
        tx: float,
        ty: float,
        *,
        duration: float = 0.20,
        jitter_radius: float = 2.0,
    ) -> None:
        """
        Move to (tx, ty) then emit micro-jitter for `duration` seconds.

        Simulates the subtle hand tremor that occurs while a human hovers
        over an element before committing to a click.
        """
        self.move(tx, ty)
        deadline = time.time() + duration
        while time.time() < deadline:
            self._move(
                self.x + random.gauss(0, jitter_radius),
                self.y + random.gauss(0, jitter_radius),
            )
            time.sleep(random.uniform(0.018, 0.055))

    def click(
        self,
        tx: float,
        ty: float,
        *,
        button: str = "left",
        hover_duration: float = 0.14,
        hold_duration: float = 0.082,
    ) -> None:
        """
        Full human-like click:
          1. Wind-mouse movement to the target
          2. Brief hover with micro-jitter
          3. mousePressed
          4. Physiological hold (human reaction time ≈ 80 ms ± 15 ms)
          5. mouseReleased
          6. Tiny post-click recoil
        """
        self.hover(tx, ty, duration=hover_duration)
        self._button("mousePressed", button)
        time.sleep(max(0.03, hold_duration + random.gauss(0, 0.015)))
        self._button("mouseReleased", button)
        # Involuntary post-click recoil — hand lifts slightly after press
        self._move(self.x + random.gauss(0, 3.5), self.y + random.gauss(0, 2.5))
        time.sleep(random.uniform(0.04, 0.11))

    def scroll(
        self,
        *,
        x: Optional[float] = None,
        y: Optional[float] = None,
        distance: int = 400,
        direction: str = "down",
        style: str = "natural",
        deadline: Optional[float] = None,
    ) -> None:
        """
        Human-like scroll using raw WheelEvents with natural burst timing.

        distance   Total CSS pixels to scroll (approximate).
        direction  'down' | 'up'
        style      'reading'  — slow bursts (3–5 ticks) with long pauses;
                               mimics a user reading content as they scroll.
                   'seeking'  — fast bursts (5–9 ticks) with short pauses;
                               mimics a user scanning for a specific element.
                   'natural'  — randomly picks reading or seeking per burst.
        deadline   Optional float from time.time().  Scroll stops when reached.

        Scroll mechanics
        ----------------
        Each burst fires N wheel events with a natural Gaussian-jittered
        deltaY.  Between bursts there is a human-length pause.  This mirrors
        how a physical mouse wheel produces groups of clicks with rest intervals
        rather than a single continuous stream of events.
        """
        scroll_x = self.x if x is None else x
        scroll_y = self.y if y is None else y

        # deltaY > 0 → scroll down (content moves up), < 0 → scroll up
        sign = 1.0 if direction == "down" else -1.0

        scrolled = 0.0

        while scrolled < distance:
            if deadline is not None and time.time() >= deadline:
                break

            # Per-burst style selection
            if style == "natural":
                burst_style = "reading" if random.random() < 0.60 else "seeking"
            else:
                burst_style = style

            if burst_style == "reading":
                burst_ticks = random.randint(2, 5)
                delta_per_tick = random.uniform(60, 110)   # px per wheel notch
                tick_delay = random.uniform(0.025, 0.075)
                post_burst_pause = random.uniform(0.28, 1.40)
            else:  # seeking
                burst_ticks = random.randint(4, 9)
                delta_per_tick = random.uniform(90, 180)
                tick_delay = random.uniform(0.012, 0.032)
                post_burst_pause = random.uniform(0.06, 0.30)

            for _ in range(burst_ticks):
                if scrolled >= distance:
                    break
                if deadline is not None and time.time() >= deadline:
                    return

                # Gaussian noise on each individual tick
                noisy_delta = max(1.0, delta_per_tick * random.gauss(1.0, 0.10))
                # Don't overshoot the requested distance
                noisy_delta = min(noisy_delta, distance - scrolled)

                self._wheel(round(scroll_x), round(scroll_y), 0.0, sign * noisy_delta)
                scrolled += noisy_delta

                tick_jitter = tick_delay * random.gauss(1.0, 0.15)
                time.sleep(max(0.005, tick_jitter))

            # Pause between bursts
            if scrolled < distance:
                remaining_time = (
                    deadline - time.time() if deadline is not None else float("inf")
                )
                time.sleep(min(post_burst_pause, max(0, remaining_time - 0.05)))

    def wander(
        self,
        duration: float = 5.0,
        *,
        waypoints: Optional[int] = None,
        scroll_probability: float = 0.25,
        scroll_distance_range: Tuple[int, int] = (80, 250),
        deadline: Optional[float] = None,
    ) -> None:
        """
        Simulate a human reading/scanning a page for `duration` seconds.

        Visits random waypoints across the viewport using wind-mouse movement,
        pausing at each one as if reading content.  Occasionally triggers a
        short downward scroll burst.

        Useful as the main behavioral simulation loop for bot-detection pages
        that score on mouse movement + scroll patterns over a fixed window
        (e.g. Incolumitas, HUMAN Security behavioral sensor).

        Parameters
        ----------
        duration              Total wall-clock seconds to keep moving.
        waypoints             Number of target waypoints to visit (None = auto).
        scroll_probability    Chance of scrolling at each waypoint pause (0–1).
        scroll_distance_range (min, max) pixels for opportunistic scrolls.
        deadline              Hard stop time (overrides duration).
        """
        self.refresh_viewport()
        stop_at = deadline if deadline is not None else time.time() + duration

        # Generate sparse waypoints across the viewport
        n_wp = waypoints if waypoints is not None else max(4, int(duration / 1.8))

        # Spread waypoints across different screen regions
        regions = [
            (0.15, 0.85, 0.10, 0.60),   # content column, upper
            (0.20, 0.80, 0.30, 0.75),   # content column, middle
            (0.25, 0.75, 0.50, 0.90),   # content column, lower
            (0.05, 0.50, 0.15, 0.65),   # left gutter
            (0.50, 0.95, 0.15, 0.65),   # right gutter
        ]
        wps: List[Point] = []
        for i in range(n_wp):
            rx1, rx2, ry1, ry2 = regions[i % len(regions)]
            wps.append((
                random.uniform(self.W * rx1, self.W * rx2),
                random.uniform(self.H * ry1, self.H * ry2),
            ))
        random.shuffle(wps)

        for wp_x, wp_y in wps:
            if time.time() >= stop_at:
                break

            # Move to waypoint
            self.move(wp_x, wp_y, method="wind")

            # Dwell: simulate reading the content near this waypoint
            remaining = stop_at - time.time()
            if remaining <= 0.2:
                break

            dwell = min(random.uniform(0.35, 1.20), remaining * 0.6)
            dwell_end = time.time() + dwell

            # Micro-jitter during dwell
            while time.time() < dwell_end:
                self._move(self.x + random.gauss(0, 1.8), self.y + random.gauss(0, 1.2))
                time.sleep(random.uniform(0.030, 0.090))

            # Opportunistic scroll during pause
            if random.random() < scroll_probability:
                remaining = stop_at - time.time()
                if remaining > 0.5:
                    sc_dist = random.randint(*scroll_distance_range)
                    sc_dir = "down" if random.random() < 0.80 else "up"
                    self.scroll(
                        distance=sc_dist,
                        direction=sc_dir,
                        style="reading",
                        deadline=stop_at,
                    )

        # Fill any remaining time with micro-jitter at current position
        while time.time() < stop_at:
            self._move(self.x + random.gauss(0, 1.5), self.y + random.gauss(0, 1.5))
            time.sleep(random.uniform(0.040, 0.110))
