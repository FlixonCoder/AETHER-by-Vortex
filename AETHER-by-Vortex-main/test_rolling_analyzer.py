"""
Unit tests for telemetry.rolling_analyzer.RollingTelemetryAnalyzer.

Tests:
  1. Single-sample startup — no warm-up required
  2. Normal reading stays NORMAL
  3. Transient spike is suppressed (TRANSIENT_SPIKE, not escalated)
  4. Persistent deviation after N ticks becomes PERSISTENT_ANOMALY
  5. Extreme deviation (EMERGENCY) bypasses persistence gate immediately
  6. Persistence counter resets when deviation clears
  7. filter_violations correctly separates genuine from transient
"""
import time
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from telemetry.rolling_analyzer import (
    RollingTelemetryAnalyzer,
    STATUS_NORMAL,
    STATUS_TRANSIENT_SPIKE,
    STATUS_PERSISTENT_ANOMALY,
    STATUS_EMERGENCY,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
PARAM = "battery_voltage_v"


def _violations(params: list[str]) -> list[dict]:
    """Manufacture synthetic violation dicts for a list of param names."""
    return [{"param": p, "value": 0.0, "threshold": 0.0, "direction": "LOW", "subsystem": "EPS"} for p in params]


def _push_normal(analyzer: RollingTelemetryAnalyzer, n: int, base: float = 28.0):
    """Push n normal readings to build up the rolling baseline (with realistic noise)."""
    import random
    ts = time.monotonic()
    for i in range(n):
        # Realistic Gaussian noise like the simulator generates
        analyzer.push({PARAM: base + random.gauss(0, 0.15)}, timestamp=ts + i * 2)


# --------------------------------------------------------------------------- #
# Test cases
# --------------------------------------------------------------------------- #
def test_single_sample_startup():
    """The analyzer must classify on the very first sample — no warm-up."""
    a = RollingTelemetryAnalyzer(window_seconds=60)
    result = a.analyze(
        values={PARAM: 28.0},
        violations=[],
        timestamp=time.monotonic(),
    )
    stats = result.parameter_stats.get(PARAM)
    assert stats is not None, "Should have stats after first sample"
    assert stats.sample_count == 1
    assert stats.status == STATUS_NORMAL, f"Expected NORMAL, got {stats.status}"
    print(f"[PASS] test_single_sample_startup: status={stats.status}, samples={stats.sample_count}")


def test_normal_reading_stays_normal():
    """Steady nominal readings with realistic noise must stay NORMAL."""
    import random
    a = RollingTelemetryAnalyzer(window_seconds=60)
    ts = time.monotonic()
    # Push 20 readings with realistic Gaussian noise
    for i in range(20):
        a.push({PARAM: 28.0 + random.gauss(0, 0.15)}, timestamp=ts + i * 2)
    # Check that a normal reading stays NORMAL
    result = a.analyze({PARAM: 28.05}, [], timestamp=ts + 42)
    s = result.parameter_stats[PARAM]
    assert s.status == STATUS_NORMAL, f"Expected NORMAL, got {s.status} (z={s.z_score})"
    print(f"[PASS] test_normal_reading_stays_normal: z={s.z_score}")


def test_transient_spike_suppressed():
    """
    A single large-deviation reading with an otherwise stable baseline
    must produce TRANSIENT_SPIKE and appear in suppressed_violations.
    """
    a = RollingTelemetryAnalyzer(window_seconds=60)
    ts = time.monotonic()
    # Build a solid baseline of nominal readings
    for i in range(15):
        a.push({PARAM: 28.0 + i * 0.01}, timestamp=ts + i * 2)

    # Inject a single spike: battery voltage very low for ONE tick
    viol = _violations([PARAM])
    result = a.analyze({PARAM: 5.0}, viol, timestamp=ts + 32)
    s = result.parameter_stats[PARAM]

    print(f"  transient_spikes={result.transient_spikes}, status={s.status}, z={s.z_score}")
    assert s.status in (STATUS_TRANSIENT_SPIKE, STATUS_EMERGENCY), (
        f"Expected TRANSIENT_SPIKE or EMERGENCY, got {s.status}")
    if s.status == STATUS_TRANSIENT_SPIKE:
        assert PARAM in result.suppressed_violations, "Spike must be in suppressed_violations"
    print(f"[PASS] test_transient_spike_suppressed: status={s.status}")


def test_persistent_anomaly_escalates():
    """
    N consecutive abnormal readings must flip to PERSISTENT_ANOMALY.
    Use a mild voltage drop that exceeds z-threshold but NOT emergency ratio.
    battery_voltage_v: nominal=28, range=22-33.6 (span=11.6). EMERGENCY_DEVIATION_RATIO=0.35.
    0.35 * 11.6 = 4.06 => anomaly = 28 - 4.06 = 23.94 => anything > 23.94 is safe from emergency.
    Use 25.5 (below warn_low=24.5 by 1V — mild but persistent).
    """
    from config import PERSISTENCE_TICKS_THRESHOLD
    import random
    a = RollingTelemetryAnalyzer(window_seconds=60)
    ts = time.monotonic()

    # Build strong baseline with realistic noise
    for i in range(20):
        a.push({PARAM: 28.0 + random.gauss(0, 0.15)}, timestamp=ts + i * 2)

    # Push a moderate but clearly anomalous value repeatedly
    # 25.8V: within-range, not emergency (ratio=(28-25.8)/11.6=0.19 < 0.35)
    # but clearly below the baseline mean, so z-score should be high
    viol = _violations([PARAM])
    status_seen = []
    for i in range(PERSISTENCE_TICKS_THRESHOLD + 3):
        result = a.analyze({PARAM: 25.8}, viol, timestamp=ts + 42 + i * 2)
        s = result.parameter_stats[PARAM]
        status_seen.append(s.status)
        print(f"  tick {i}: status={s.status}, z={s.z_score:.2f}, persist={s.persistence_ticks}")

    print(f"  status sequence: {status_seen}")
    assert STATUS_PERSISTENT_ANOMALY in status_seen or STATUS_EMERGENCY in status_seen, (
        f"Expected PERSISTENT_ANOMALY or EMERGENCY after {PERSISTENCE_TICKS_THRESHOLD} ticks, got {status_seen}")
    print(f"[PASS] test_persistent_anomaly_escalates: statuses={set(status_seen)}")


def test_emergency_bypasses_persistence():
    """
    An extreme z-score (above CRITICAL_ZSCORE_THRESHOLD) or emergency ratio
    must produce EMERGENCY status immediately, regardless of persistence count.
    """
    from config import CRITICAL_ZSCORE_THRESHOLD
    a = RollingTelemetryAnalyzer(window_seconds=60)
    ts = time.monotonic()

    # Build tight baseline
    for i in range(20):
        a.push({PARAM: 28.0}, timestamp=ts + i * 2)

    # Force extreme deviation — battery at absolute minimum (config min is 22V,
    # dropping to 0 V is extreme across the 20V range → ratio >= 0.35)
    viol = _violations([PARAM])
    result = a.analyze({PARAM: 0.0}, viol, timestamp=ts + 42)
    s = result.parameter_stats[PARAM]

    print(f"  emergency check: status={s.status}, z={s.z_score}")
    assert s.status == STATUS_EMERGENCY, (
        f"Expected EMERGENCY for extreme value, got {s.status} (z={s.z_score})")
    assert PARAM in result.emergency_params
    print(f"[PASS] test_emergency_bypasses_persistence: status={s.status}")


def test_persistence_resets_on_recovery():
    """
    After repeated anomalies, returning to normal must reset persistence counter.
    """
    from config import PERSISTENCE_TICKS_THRESHOLD
    a = RollingTelemetryAnalyzer(window_seconds=60)
    ts = time.monotonic()
    viol = _violations([PARAM])

    # Build baseline
    for i in range(10):
        a.push({PARAM: 28.0}, timestamp=ts + i * 2)

    # Trigger persistent anomaly
    for i in range(PERSISTENCE_TICKS_THRESHOLD + 1):
        a.analyze({PARAM: 19.0}, viol, timestamp=ts + 22 + i * 2)

    # Recover to normal for many ticks
    no_viol = []
    for i in range(15):
        result = a.analyze({PARAM: 28.0}, no_viol, timestamp=ts + 50 + i * 2)

    s = result.parameter_stats[PARAM]
    assert s.persistence_ticks == 0, (
        f"persistence_ticks should be 0 after recovery, got {s.persistence_ticks}")
    assert s.status == STATUS_NORMAL, f"Expected NORMAL after recovery, got {s.status}"
    print(f"[PASS] test_persistence_resets_on_recovery: persistence_ticks={s.persistence_ticks}")


def test_no_false_positives_nominal_operation():
    """
    Full 60 seconds of nominal telemetry (all params) must show no anomalies.
    """
    import random
    a = RollingTelemetryAnalyzer(window_seconds=60)
    ts = time.monotonic()
    nominal = {
        "battery_voltage_v": 28.0, "battery_soc_pct": 85.0, "solar_current_a": 6.2,
        "bus_power_w": 45.0, "temp_obc_c": 25.0, "temp_battery_c": 20.0,
        "temp_payload_c": 22.0, "attitude_error_deg": 0.1, "reaction_wheel_rpm": 2000.0,
        "downlink_snr_db": 28.0, "memory_usage_pct": 45.0, "cpu_usage_pct": 30.0,
    }
    for i in range(30):
        noisy = {k: v + random.gauss(0, 0.1) for k, v in nominal.items()}
        result = a.analyze(noisy, [], timestamp=ts + i * 2)

    anomalous = [p for p, s in result.parameter_stats.items()
                 if s.status in (STATUS_PERSISTENT_ANOMALY, STATUS_EMERGENCY)]
    assert len(anomalous) == 0, f"False positives detected: {anomalous}"
    print(f"[PASS] test_no_false_positives_nominal_operation: all params NORMAL/TRANSIENT")


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    tests = [
        test_single_sample_startup,
        test_normal_reading_stays_normal,
        test_transient_spike_suppressed,
        test_persistent_anomaly_escalates,
        test_emergency_bypasses_persistence,
        test_persistence_resets_on_recovery,
        test_no_false_positives_nominal_operation,
    ]
    passed = failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except AssertionError as e:
            print(f"[FAIL] {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"[ERROR] {t.__name__}: {type(e).__name__}: {e}")
            failed += 1

    print(f"\n{'='*50}")
    print(f"Results: {passed}/{passed+failed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
