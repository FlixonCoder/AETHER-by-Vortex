"""Signal conditioning for AETHER telemetry.

Separation of concerns:
    - Persistence decides WHETHER a reading is a fault.
    - The rolling window describes WHAT the fault looks like.

Detection never uses a window mean. A window mean delays step-change
detection by roughly half the window; at a 60 s window and 50-90 s
anomalies that is most of the event. Detection uses instantaneous limit
checks plus a persistence counter, which is how flight FDIR actually
works (ESA PUS Service 12 "persistency").

All persistence is TIME-WINDOWED, not sample-counted. Sample counting
assumes uniform cadence, which is false for replayed ground-station
telemetry where consecutive frames can be days apart.

Baselines are FROZEN on entry to SUSPECT. A Z-score computed against a
window that already contains the excursion drifts toward the anomaly and
collapses -- measured: 2.55 -> 0.50 while the fault is still present.
"""

from __future__ import annotations

import math
import statistics as stats
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Iterable

# Scale factor converting MAD to a standard-deviation-equivalent for a
# normal distribution. Used so Z stays interpretable as "sigmas".
_MAD_TO_SIGMA = 1.4826

# Floor on the robust scale estimate. Without it, a perfectly flat signal
# gives MAD == 0 and every Z becomes infinite.
_MIN_SCALE = 1e-6


class ParamState(str, Enum):
    """Per-parameter monitor state."""

    NOMINAL = "NOMINAL"
    SUSPECT = "SUSPECT"
    CONFIRMED = "CONFIRMED"
    CLEARING = "CLEARING"
    STALE = "STALE"


class ConfirmPath(str, Enum):
    """Why a parameter reached CONFIRMED."""

    PERSISTENCE = "PERSISTENCE"
    RATE = "RATE"


@dataclass(frozen=True)
class Sample:
    ts: float
    value: float


@dataclass(frozen=True)
class Baseline:
    """Last-known-good statistics, frozen at SUSPECT entry."""

    median: float
    scale: float
    n: int

    def z(self, value: float) -> float:
        return abs(value - self.median) / max(self.scale, _MIN_SCALE)


@dataclass(frozen=True)
class WindowStats:
    """Characterisation of the current window. Never used for detection."""

    n: int
    span_s: float
    sma: float
    ema: float
    median: float
    mad: float
    stdev: float
    minimum: float
    maximum: float
    slope_per_s: float
    median_gap_s: float
    max_gap_s: float

    @property
    def trend(self) -> str:
        """Computed, not guessed by a model."""
        if abs(self.slope_per_s) < 1e-9:
            return "stable"
        return "rising" if self.slope_per_s > 0 else "falling"


@dataclass(frozen=True)
class ParamReading:
    """One conditioned parameter at one tick."""

    name: str
    value: float
    ts: float
    state: ParamState
    out_of_limit: bool
    z: float | None
    stats: WindowStats | None
    baseline: Baseline | None
    confirmed_via: ConfirmPath | None
    violation_count: int
    gap_s: float | None

    @property
    def is_actionable(self) -> bool:
        # CLEARING counts. It means "was confirmed, still waiting for enough
        # good readings to believe it" -- the event is not over yet. Gating on
        # CONFIRMED alone reopens the chatter clear_count exists to stop: a
        # signal resting on its limit alternates CONFIRMED/CLEARING every
        # sample, and the escalation gate toggles with it.
        return self.state in (ParamState.CONFIRMED, ParamState.CLEARING)


@dataclass
class ParamLimits:
    """Detection configuration for one parameter."""

    warn_low: float | None = None
    warn_high: float | None = None
    # Absolute rate of change that is itself a fault, in units/second.
    # None disables the rate confirm path.
    rate_limit_per_s: float | None = None
    # The rate path needs a well-populated window before it is trusted. A
    # least-squares slope over 3 points has enormous variance: measured on
    # nominal bus voltage, 3 samples across 4 s gave a 3-sigma noise slope of
    # 0.096 V/s against a 0.15 V/s limit, and duly confirmed a fault on a
    # perfectly healthy 28.5 V spacecraft two ticks after boot. At 30 s the
    # same figure is 0.0075 V/s.
    rate_min_span_s: float = 30.0
    rate_min_samples: int = 10
    # Violations required inside confirm_window_s to confirm.
    confirm_count: int = 3
    confirm_window_s: float = 8.0
    # In-limit readings required to clear. Deliberately larger than
    # confirm_count: harder to clear than to trip, which kills chatter
    # at the threshold boundary.
    clear_count: int = 5
    # Window the clear_count must fall inside. Separate from
    # confirm_window_s: sharing one window silently couples "trip fast"
    # to "clear slow" and, at 2 s cadence, leaves clearing needing 5
    # samples in an 8 s span -- exactly 5 fit, so a single late frame
    # would strand the parameter in CLEARING forever.
    clear_window_s: float = 20.0
    # No sample for this long -> STALE. Persistence never spans a gap.
    stale_after_s: float = 30.0

    def violates(self, value: float) -> bool:
        if self.warn_low is not None and value < self.warn_low:
            return True
        if self.warn_high is not None and value > self.warn_high:
            return True
        return False


def _robust_scale(values: Iterable[float], centre: float) -> float:
    deviations = [abs(v - centre) for v in values]
    if not deviations:
        return _MIN_SCALE
    return max(_MAD_TO_SIGMA * stats.median(deviations), _MIN_SCALE)


def _slope_per_s(samples: list[Sample]) -> float:
    """Least-squares slope in units/second. Zero when time does not vary."""
    if len(samples) < 2:
        return 0.0
    t0 = samples[0].ts
    xs = [s.ts - t0 for s in samples]
    ys = [s.value for s in samples]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0.0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom


class ParameterBuffer:
    """Rolling time window and monitor state machine for one parameter."""

    def __init__(
        self,
        name: str,
        limits: ParamLimits,
        window_s: float = 60.0,
        ema_alpha: float = 0.2,
        max_samples: int = 256,
    ) -> None:
        if window_s <= 0:
            raise ValueError(f"{name}: window_s must be positive")
        if not 0.0 < ema_alpha <= 1.0:
            raise ValueError(f"{name}: ema_alpha must be in (0, 1]")
        if limits.warn_low is None and limits.warn_high is None:
            if limits.rate_limit_per_s is None:
                raise ValueError(
                    f"{name}: parameter has no warn_low, no warn_high and no "
                    "rate_limit_per_s -- it can never produce a detection. "
                    "Give it a limit or remove it from the monitored set."
                )

        self.name = name
        self.limits = limits
        self.window_s = window_s
        self.ema_alpha = ema_alpha

        self._samples: Deque[Sample] = deque(maxlen=max_samples)
        self._ema: float | None = None
        self._state = ParamState.NOMINAL
        self._baseline: Baseline | None = None
        self._confirmed_via: ConfirmPath | None = None
        # (ts, violated) history used for time-windowed persistence.
        self._events: Deque[tuple[float, bool]] = deque(maxlen=max_samples)

    @property
    def state(self) -> ParamState:
        return self._state

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_s
        while self._samples and self._samples[0].ts < cutoff:
            self._samples.popleft()
        # Events are aged on the longer of the two persistence windows, not
        # on window_s: trimming them to the stats window would cap how far
        # back a clear-count could ever look.
        ev_cutoff = now - max(
            self.window_s,
            self.limits.confirm_window_s,
            self.limits.clear_window_s,
        )
        while self._events and self._events[0][0] < ev_cutoff:
            self._events.popleft()

    def _window_stats(self) -> WindowStats | None:
        if not self._samples:
            return None
        values = [s.value for s in self._samples]
        median = stats.median(values)
        gaps = [
            b.ts - a.ts for a, b in zip(self._samples, list(self._samples)[1:])
        ]
        return WindowStats(
            n=len(values),
            span_s=self._samples[-1].ts - self._samples[0].ts,
            sma=sum(values) / len(values),
            ema=self._ema if self._ema is not None else values[-1],
            median=median,
            mad=stats.median([abs(v - median) for v in values]),
            stdev=stats.pstdev(values) if len(values) > 1 else 0.0,
            minimum=min(values),
            maximum=max(values),
            slope_per_s=_slope_per_s(list(self._samples)),
            median_gap_s=stats.median(gaps) if gaps else 0.0,
            max_gap_s=max(gaps) if gaps else 0.0,
        )

    def _freeze_baseline(self) -> Baseline:
        """Snapshot last-known-good stats, excluding the current sample."""
        clean = [s.value for s in list(self._samples)[:-1]]
        if not clean:
            clean = [self._samples[-1].value]
        median = stats.median(clean)
        return Baseline(
            median=median, scale=_robust_scale(clean, median), n=len(clean)
        )

    def _count_recent(self, now: float, violated: bool, window_s: float) -> int:
        cutoff = now - window_s
        return sum(1 for ts, v in self._events if ts >= cutoff and v is violated)

    def update(self, value: float, ts: float) -> ParamReading:
        """Ingest one sample and advance the state machine."""
        if not math.isfinite(value):
            raise ValueError(f"{self.name}: non-finite telemetry value {value!r}")

        gap: float | None = None
        if self._samples:
            gap = ts - self._samples[-1].ts
            if gap < 0:
                raise ValueError(
                    f"{self.name}: sample went backwards in time "
                    f"({ts} after {self._samples[-1].ts})"
                )

        # A gap wider than stale_after_s breaks persistence. Counting across
        # it would fabricate a confirmation from readings taken far apart.
        stale_break = gap is not None and gap > self.limits.stale_after_s
        if stale_break:
            self._events.clear()
            if self._state in (ParamState.SUSPECT, ParamState.CLEARING):
                self._state = ParamState.STALE
                self._baseline = None

        self._samples.append(Sample(ts=ts, value=value))
        self._ema = (
            value
            if self._ema is None
            else self.ema_alpha * value + (1.0 - self.ema_alpha) * self._ema
        )
        self._trim(ts)

        violated = self.limits.violates(value)
        self._events.append((ts, violated))

        window = self._window_stats()
        rate_fault = (
            self.limits.rate_limit_per_s is not None
            and window is not None
            and window.n >= self.limits.rate_min_samples
            and window.span_s >= self.limits.rate_min_span_s
            and abs(window.slope_per_s) > self.limits.rate_limit_per_s
        )

        if self._state is ParamState.STALE and not stale_break:
            self._state = ParamState.NOMINAL

        if self._state in (ParamState.NOMINAL, ParamState.STALE):
            if violated or rate_fault:
                self._baseline = self._freeze_baseline()
                self._state = ParamState.SUSPECT

        if self._state is ParamState.SUSPECT:
            # Rate confirms immediately: a steep ramp is a fault before it
            # ever crosses a static limit.
            if rate_fault:
                self._state = ParamState.CONFIRMED
                self._confirmed_via = ConfirmPath.RATE
            elif (
                self._count_recent(ts, True, self.limits.confirm_window_s)
                >= self.limits.confirm_count
            ):
                self._state = ParamState.CONFIRMED
                self._confirmed_via = ConfirmPath.PERSISTENCE
            elif not violated:
                # Excursion ended before reaching confirm_count. There is
                # exactly one threshold, so there is no undefined middle
                # band: this is a transient, full stop.
                self._state = ParamState.NOMINAL
                self._baseline = None

        elif self._state is ParamState.CONFIRMED:
            if not violated and not rate_fault:
                self._state = ParamState.CLEARING

        elif self._state is ParamState.CLEARING:
            if violated or rate_fault:
                self._state = ParamState.CONFIRMED
            elif (
                self._count_recent(ts, False, self.limits.clear_window_s)
                >= self.limits.clear_count
            ):
                self._state = ParamState.NOMINAL
                self._baseline = None
                self._confirmed_via = None

        return ParamReading(
            name=self.name,
            value=value,
            ts=ts,
            state=self._state,
            out_of_limit=violated,
            z=self._baseline.z(value) if self._baseline else None,
            stats=window,
            baseline=self._baseline,
            confirmed_via=(
                self._confirmed_via
                if self._state
                in (ParamState.CONFIRMED, ParamState.CLEARING)
                else None
            ),
            violation_count=self._count_recent(
                ts, True, self.limits.confirm_window_s
            ),
            gap_s=gap,
        )


@dataclass
class ConditionedFrame:
    """Everything the pipeline needs from one telemetry tick."""

    ts: float
    readings: dict[str, ParamReading] = field(default_factory=dict)

    @property
    def confirmed(self) -> list[ParamReading]:
        return [r for r in self.readings.values() if r.is_actionable]

    @property
    def suspect(self) -> list[ParamReading]:
        return [
            r for r in self.readings.values() if r.state is ParamState.SUSPECT
        ]

    @property
    def stale(self) -> list[ParamReading]:
        return [r for r in self.readings.values() if r.state is ParamState.STALE]

    @property
    def should_escalate(self) -> bool:
        """The only gate to the LLM pipeline."""
        return bool(self.confirmed)

    def evidence(self) -> list[str]:
        """Human-readable justification, for prompts and operator logs."""
        lines = []
        for r in self.confirmed:
            z = f"{r.z:.1f}" if r.z is not None else "n/a"
            lines.append(
                f"{r.name}={r.value:.3f} "
                f"(Z={z} vs frozen baseline, "
                f"confirmed via {r.confirmed_via.value.lower()}, "
                f"{r.violation_count} violations in "
                f"{r.stats.span_s:.0f}s window)"
                if r.stats
                else f"{r.name}={r.value:.3f} (Z={z})"
            )
        return lines


class TelemetryConditioner:
    """Owns one ParameterBuffer per monitored parameter."""

    def __init__(self, limits: dict[str, ParamLimits], window_s: float = 60.0):
        if not limits:
            raise ValueError("TelemetryConditioner requires at least one parameter")
        self._buffers = {
            name: ParameterBuffer(name, lim, window_s=window_s)
            for name, lim in limits.items()
        }

    def ingest(self, values: dict[str, float], ts: float) -> ConditionedFrame:
        unknown = set(values) - set(self._buffers)
        if unknown:
            raise KeyError(
                f"Telemetry contains unmonitored parameters: {sorted(unknown)}. "
                "Add limits to config or drop them before ingest."
            )
        frame = ConditionedFrame(ts=ts)
        for name, value in values.items():
            frame.readings[name] = self._buffers[name].update(value, ts)
        return frame

    def state_of(self, name: str) -> ParamState:
        return self._buffers[name].state
