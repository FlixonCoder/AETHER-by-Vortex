"""
Rolling Telemetry Analyzer — 60-second statistical anomaly filter.

Maintains a per-parameter rolling window of the last N seconds of readings
and distinguishes:
  - NORMAL            : value within expected statistical variation
  - TRANSIENT_SPIKE   : single/few abnormal readings within normal rolling window
  - PERSISTENT_ANOMALY: sustained deviation exceeding persistence threshold

Key design rules
  * Operates immediately from the first sample — no warm-up period.
  * EMERGENCY category (extreme z-score or ratio deviation) always bypasses
    persistence gating.
  * Configurable via config.py constants (ROLLING_WINDOW_SECONDS,
    ROLLING_MIN_SAMPLES, SPIKE_ZSCORE_THRESHOLD, PERSISTENCE_TICKS_THRESHOLD,
    CRITICAL_ZSCORE_THRESHOLD, EMERGENCY_DEVIATION_RATIO).
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import (
    CRITICAL_ZSCORE_THRESHOLD,
    EMERGENCY_DEVIATION_RATIO,
    PERSISTENCE_TICKS_THRESHOLD,
    ROLLING_MIN_SAMPLES,
    ROLLING_WINDOW_SECONDS,
    SPIKE_ZSCORE_THRESHOLD,
    TELEMETRY_PARAMS,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants & Classification Labels
# ─────────────────────────────────────────────────────────────────────────────
STATUS_NORMAL = "NORMAL"
STATUS_TRANSIENT_SPIKE = "TRANSIENT_SPIKE"
STATUS_PERSISTENT_ANOMALY = "PERSISTENT_ANOMALY"
STATUS_EMERGENCY = "EMERGENCY"  # Extreme deviation — always escalated


@dataclass
class ParameterStats:
    """Per-parameter rolling statistics snapshot."""
    param: str
    current_value: float
    mean: float
    std_dev: float
    minimum: float
    maximum: float
    rate_of_change: float       # units/second
    z_score: float              # z-score of current value vs rolling mean
    sample_count: int
    persistence_ticks: int      # consecutive ticks exceeding z-score threshold
    status: str                 # STATUS_* constant
    window_seconds: float       # actual span of samples in the window


@dataclass
class RollingAnalysisResult:
    """Result of a full telemetry snapshot analysis."""
    timestamp: float
    parameter_stats: Dict[str, ParameterStats]
    persistent_anomalies: List[str]   # param names with PERSISTENT_ANOMALY
    transient_spikes: List[str]       # param names with TRANSIENT_SPIKE
    emergency_params: List[str]       # param names with EMERGENCY status
    suppressed_violations: List[str]  # violations suppressed as transient


# ─────────────────────────────────────────────────────────────────────────────
# Per-Parameter Rolling Buffer
# ─────────────────────────────────────────────────────────────────────────────
class _ParameterBuffer:
    """Timestamped circular buffer for a single telemetry parameter."""

    def __init__(self, window_seconds: float):
        self._window_seconds = window_seconds
        # Each entry: (timestamp_s, value)
        self._buf: deque = deque()
        self._persistence_ticks: int = 0

    def push(self, value: float, timestamp: Optional[float] = None) -> None:
        """Add a new reading and prune samples older than the window."""
        ts = timestamp if timestamp is not None else time.monotonic()
        self._buf.append((ts, value))
        cutoff = ts - self._window_seconds
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()

    def compute_stats(self, param: str) -> ParameterStats:
        """Return a fully computed ParameterStats for the current window."""
        n = len(self._buf)

        if n == 0:
            return ParameterStats(
                param=param, current_value=0.0, mean=0.0, std_dev=0.0,
                minimum=0.0, maximum=0.0, rate_of_change=0.0, z_score=0.0,
                sample_count=0, persistence_ticks=self._persistence_ticks,
                status=STATUS_NORMAL, window_seconds=0.0
            )

        values = [v for _, v in self._buf]
        times = [t for t, _ in self._buf]
        current = values[-1]
        mean = sum(values) / n

        variance = sum((v - mean) ** 2 for v in values) / n
        std_dev = math.sqrt(variance)

        window_span = times[-1] - times[0] if n > 1 else 0.0

        # Rate of change (per second) — last sample vs earliest in window
        if n > 1 and window_span > 0:
            roc = (values[-1] - values[0]) / window_span
        else:
            roc = 0.0

        # Z-score
        z = (current - mean) / std_dev if std_dev > 1e-9 else 0.0

        # ------------------------------------------------------------------
        # Classification
        # ------------------------------------------------------------------
        param_cfg = TELEMETRY_PARAMS.get(param, {})
        nominal = param_cfg.get("nominal", mean)
        param_range = (param_cfg.get("max", nominal) - param_cfg.get("min", nominal)) or 1.0
        emergency_ratio = abs(current - nominal) / abs(param_range)

        is_emergency = (
            abs(z) >= CRITICAL_ZSCORE_THRESHOLD or
            emergency_ratio >= EMERGENCY_DEVIATION_RATIO
        )

        is_anomalous_reading = abs(z) >= SPIKE_ZSCORE_THRESHOLD

        if is_anomalous_reading or is_emergency:
            self._persistence_ticks += 1
        else:
            self._persistence_ticks = 0

        # Classify
        if is_emergency:
            status = STATUS_EMERGENCY
        elif n < ROLLING_MIN_SAMPLES:
            # Not enough samples yet — classify transient rather than persist
            status = STATUS_TRANSIENT_SPIKE if is_anomalous_reading else STATUS_NORMAL
        elif is_anomalous_reading and self._persistence_ticks >= PERSISTENCE_TICKS_THRESHOLD:
            status = STATUS_PERSISTENT_ANOMALY
        elif is_anomalous_reading:
            status = STATUS_TRANSIENT_SPIKE
        else:
            status = STATUS_NORMAL

        return ParameterStats(
            param=param,
            current_value=round(current, 4),
            mean=round(mean, 4),
            std_dev=round(std_dev, 4),
            minimum=round(min(values), 4),
            maximum=round(max(values), 4),
            rate_of_change=round(roc, 6),
            z_score=round(z, 3),
            sample_count=n,
            persistence_ticks=self._persistence_ticks,
            status=status,
            window_seconds=round(window_span, 1),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main Analyzer
# ─────────────────────────────────────────────────────────────────────────────
class RollingTelemetryAnalyzer:
    """
    Maintains a per-parameter rolling window and analyses each snapshot.

    Usage (called once per telemetry tick):
        result = analyzer.analyze(snapshot_values, violations, tick_timestamp)
        genuine = result.persistent_anomalies + result.emergency_params
        suppressed = result.suppressed_violations
    """

    def __init__(self, window_seconds: float = ROLLING_WINDOW_SECONDS):
        self._window_seconds = window_seconds
        self._buffers: Dict[str, _ParameterBuffer] = {}
        # Params currently latched as a confirmed, ongoing persistent anomaly.
        # See the long comment in analyze() for why this exists.
        self._latched: set = set()

    def _get_buffer(self, param: str) -> _ParameterBuffer:
        if param not in self._buffers:
            self._buffers[param] = _ParameterBuffer(self._window_seconds)
        return self._buffers[param]

    def push(self, values: Dict[str, float], timestamp: Optional[float] = None) -> None:
        """Ingest one telemetry snapshot into the rolling buffers."""
        ts = timestamp if timestamp is not None else time.monotonic()
        for param, value in values.items():
            self._get_buffer(param).push(value, ts)

    def analyze(
        self,
        values: Dict[str, float],
        violations: List[dict],
        timestamp: Optional[float] = None,
    ) -> RollingAnalysisResult:
        """
        Push values, compute stats, and classify each violated parameter.

        Returns a RollingAnalysisResult with:
          - persistent_anomalies: violations confirmed as genuine (pass through)
          - transient_spikes: violations suppressed as noise
          - emergency_params: extreme deviations that always escalate
          - suppressed_violations: list of violation param names suppressed
        """
        ts = timestamp if timestamp is not None else time.monotonic()
        self.push(values, ts)

        param_stats: Dict[str, ParameterStats] = {}
        for param in values:
            stats = self._get_buffer(param).compute_stats(param)
            param_stats[param] = stats

        persistent_anomalies: List[str] = []
        transient_spikes: List[str] = []
        emergency_params: List[str] = []
        suppressed_violations: List[str] = []

        violated_params = {v["param"] for v in violations}

        # Latch handling: the z-score in ParameterStats is computed against
        # THIS param's own rolling window -- which keeps ingesting the fault's
        # readings every tick. For a fault that holds steady (rather than
        # getting worse) past the initial ~PERSISTENCE_TICKS_THRESHOLD ticks,
        # the window's own mean/std drift to re-center on the depressed
        # value, so the z-score decays back toward 0 and status falls from
        # PERSISTENT_ANOMALY back to TRANSIENT_SPIKE/NORMAL -- NOT because the
        # fault cleared, but because the rolling baseline got contaminated by
        # the fault it's supposed to be measuring against. Unpatched, this
        # meant a real, still-out-of-band fault could silently stop being
        # reported a few ticks after its first (correct) detection, and if
        # the orchestrator's periodic check happened to land after that
        # window closed, the fault was never escalated to the agent pipeline
        # at all -- confirmed against a real battery_undervoltage trace.
        #
        # Fix: once a param first proves itself PERSISTENT_ANOMALY (still via
        # the normal 3-consecutive-tick z-score gate -- pure noise still gets
        # filtered exactly as before), latch it. While latched, a param stays
        # classified as persistent for as long as it remains in `violations`
        # (the actual, non-self-referential TELEMETRY_PARAMS warn-threshold
        # breach) -- regardless of what the rolling z-score says. The latch
        # only clears once the param is no longer in `violations`, i.e. it
        # has genuinely returned within its warn band.
        for param in list(self._latched):
            if param not in violated_params:
                self._latched.discard(param)

        for param in violated_params:
            stats = param_stats.get(param)
            if stats is None:
                persistent_anomalies.append(param)
                continue

            if stats.status == STATUS_EMERGENCY:
                emergency_params.append(param)
                self._latched.add(param)
            elif stats.status == STATUS_PERSISTENT_ANOMALY:
                persistent_anomalies.append(param)
                self._latched.add(param)
            elif param in self._latched:
                persistent_anomalies.append(param)
            elif stats.status == STATUS_TRANSIENT_SPIKE:
                transient_spikes.append(param)
                suppressed_violations.append(param)
            else:
                suppressed_violations.append(param)

        return RollingAnalysisResult(
            timestamp=ts,
            parameter_stats=param_stats,
            persistent_anomalies=persistent_anomalies,
            transient_spikes=transient_spikes,
            emergency_params=emergency_params,
            suppressed_violations=suppressed_violations,
        )

    def get_summary(self) -> Dict[str, dict]:
        """
        Return a serialisable summary of current rolling stats for all
        tracked parameters — used by the API and WebSocket broadcast.
        """
        out: Dict[str, dict] = {}
        for param, buf in self._buffers.items():
            s = buf.compute_stats(param)
            out[param] = {
                "status": s.status,
                "current": s.current_value,
                "mean": s.mean,
                "std_dev": s.std_dev,
                "min": s.minimum,
                "max": s.maximum,
                "rate_of_change": s.rate_of_change,
                "z_score": s.z_score,
                "sample_count": s.sample_count,
                "persistence_ticks": s.persistence_ticks,
                "window_seconds": s.window_seconds,
            }
        return out

    def reset_param(self, param: str) -> None:
        """Clear the rolling buffer for a single parameter."""
        if param in self._buffers:
            del self._buffers[param]

    def reset_all(self) -> None:
        """Clear all rolling buffers (e.g. after anomaly scenario clears)."""
        self._buffers.clear()
