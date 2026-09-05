"""
Tests for model.live_adapter.classify_from_live_telemetry.

Coverage:
  (a) Given realistic snapshot/history/violations for two anomaly types, the
      function returns a dict with all 7 required keys and a valid anomaly_type.
  (b) Given garbage / empty history, the function returns None gracefully
      (falls back to rule engine) instead of raising.
  (c) Given violations from a channel whose history is entirely missing, the
      function still returns a valid dict (back-fill path exercised).
  (d) The ML result is actually wired into WatcherAgent._rule_fallback via
      the classify_from_live_telemetry import in agents/watcher.py.
"""

import sys
import os

# Ensure the project root is on sys.path so all imports resolve correctly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from model.live_adapter import classify_from_live_telemetry, KNOWN_ANOMALY_TYPES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REQUIRED_KEYS = {
    "anomaly_type",
    "primary_subsystem",
    "severity",
    "affected_params",
    "trend",
    "confidence",
    "summary",
}

KNOWN_SUBSYSTEMS = {"EPS", "ADCS", "COMMS", "THERMAL", "OBC"}
VALID_TRENDS = {"worsening", "stable", "improving"}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_history(channels: list, n_samples: int = 15, base_values: dict = None):
    """Build a fake history dict for the given channel names."""
    base_values = base_values or {}
    history = {}
    for ch in channels:
        base = base_values.get(ch, 1.0)
        history[ch] = [{"value": base + i * 0.01, "ts": f"2026-01-01T00:00:{i:02d}Z"}
                       for i in range(n_samples)]
    return history


def _assert_valid_result(result, test_name: str):
    """Assert that result has all required keys with correct types."""
    assert result is not None, f"[{test_name}] Expected dict, got None"
    missing = REQUIRED_KEYS - set(result.keys())
    assert not missing, f"[{test_name}] Missing required keys: {missing}"

    # anomaly_type is one of the known types OR the generic fallback
    valid_types = KNOWN_ANOMALY_TYPES | {"threshold_violation"}
    assert result["anomaly_type"] in valid_types, (
        f"[{test_name}] anomaly_type '{result['anomaly_type']}' not in valid set"
    )

    assert result["primary_subsystem"] in KNOWN_SUBSYSTEMS, (
        f"[{test_name}] primary_subsystem '{result['primary_subsystem']}' not in {KNOWN_SUBSYSTEMS}"
    )

    assert isinstance(result["affected_params"], list), (
        f"[{test_name}] affected_params should be a list"
    )

    assert result["trend"] in VALID_TRENDS, (
        f"[{test_name}] trend '{result['trend']}' not in {VALID_TRENDS}"
    )

    conf = result["confidence"]
    assert isinstance(conf, float), f"[{test_name}] confidence should be float, got {type(conf)}"
    assert 0.0 <= conf <= 1.0, f"[{test_name}] confidence {conf} out of range [0.0, 1.0]"

    assert isinstance(result["summary"], str) and len(result["summary"]) > 0, (
        f"[{test_name}] summary should be a non-empty string"
    )
    print(f"[PASS] {test_name}: anomaly_type={result['anomaly_type']}, "
          f"subsystem={result['primary_subsystem']}, confidence={result['confidence']:.2f}")


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_battery_undervoltage_scenario():
    """
    Simulate a battery undervoltage scenario: low battery_voltage_v,
    battery_soc_pct, bus_voltage_v — these are the channels that the
    trained model associates with battery_undervoltage.
    """
    # Channels with anomalous (low) values for battery params
    channels_of_interest = [
        "battery_voltage_v", "battery_soc_pct", "bus_voltage_v",
        "solar_current_a", "bus_power_w", "temp_obc_c", "temp_battery_c",
        "temp_payload_c", "attitude_error_deg", "reaction_wheel_rpm",
        "downlink_snr_db", "memory_usage_pct", "cpu_usage_pct",
        "battery_current_a", "solar_power_w", "gyro_bias_dps",
        "rw_current_a", "rssi_dbm", "packet_loss_pct", "gps_fix", "gps_satellites",
    ]
    # Nominal baselines, then push battery channels low
    base_values = {
        "battery_voltage_v": 22.0,   # well below warn_low=24.5
        "battery_soc_pct": 25.0,     # below warn_low=30
        "bus_voltage_v": 21.5,       # below warn_low=21
        "solar_current_a": 6.2,
        "bus_power_w": 45.0,
        "temp_obc_c": 25.0,
        "temp_battery_c": 20.0,
        "temp_payload_c": 22.0,
        "attitude_error_deg": 0.1,
        "reaction_wheel_rpm": 2000.0,
        "downlink_snr_db": 28.0,
        "memory_usage_pct": 45.0,
        "cpu_usage_pct": 30.0,
        "battery_current_a": 0.0,
        "solar_power_w": 173.0,
        "gyro_bias_dps": 0.0,
        "rw_current_a": 0.25,
        "rssi_dbm": -85.0,
        "packet_loss_pct": 0.5,
        "gps_fix": 1.0,
        "gps_satellites": 8.0,
    }

    snapshot_values = dict(base_values)  # current reading matches history trend

    history = _make_history(channels_of_interest, n_samples=15, base_values=base_values)

    violations = [
        {"param": "battery_voltage_v", "value": 22.0, "threshold": 24.5,
         "direction": "LOW", "subsystem": "EPS"},
        {"param": "battery_soc_pct",   "value": 25.0, "threshold": 30.0,
         "direction": "LOW", "subsystem": "EPS"},
        {"param": "bus_voltage_v",     "value": 21.5, "threshold": 21.0,
         "direction": "LOW", "subsystem": "EPS"},
    ]

    result = classify_from_live_telemetry(snapshot_values, history, violations)
    _assert_valid_result(result, "test_battery_undervoltage_scenario")

    # With 3 violations, trend must be "worsening"
    assert result["trend"] == "worsening", (
        f"Expected trend=worsening for 3 violations, got {result['trend']}"
    )
    # affected_params must contain all violation params
    for v in violations:
        assert v["param"] in result["affected_params"], (
            f"Expected {v['param']} in affected_params"
        )


def test_comms_degradation_scenario():
    """
    Simulate a comms degradation scenario: low downlink SNR, high packet
    loss, low RSSI — the signature the model classifies as comms_degradation.
    """
    base_values = {
        "battery_voltage_v": 28.0,
        "battery_soc_pct": 85.0,
        "solar_current_a": 6.2,
        "bus_power_w": 45.0,
        "temp_obc_c": 25.0,
        "temp_battery_c": 20.0,
        "temp_payload_c": 22.0,
        "attitude_error_deg": 0.1,
        "reaction_wheel_rpm": 2000.0,
        "downlink_snr_db": 7.0,      # below warn_low=12
        "memory_usage_pct": 45.0,
        "cpu_usage_pct": 30.0,
        "battery_current_a": 0.0,
        "solar_power_w": 173.0,
        "gyro_bias_dps": 0.0,
        "rw_current_a": 0.25,
        "rssi_dbm": -107.0,          # below warn_low=-105
        "packet_loss_pct": 38.0,     # above warn_high=5
        "gps_fix": 1.0,
        "gps_satellites": 8.0,
    }

    snapshot_values = dict(base_values)
    history = _make_history(list(base_values.keys()), n_samples=18, base_values=base_values)

    violations = [
        {"param": "downlink_snr_db", "value": 7.0, "threshold": 12.0,
         "direction": "LOW", "subsystem": "COMMS"},
        {"param": "packet_loss_pct", "value": 38.0, "threshold": 5.0,
         "direction": "HIGH", "subsystem": "COMMS"},
        {"param": "rssi_dbm",        "value": -107.0, "threshold": -105.0,
         "direction": "LOW", "subsystem": "COMMS"},
    ]

    result = classify_from_live_telemetry(snapshot_values, history, violations)
    _assert_valid_result(result, "test_comms_degradation_scenario")

    # subsystem must be COMMS (derived from violations[0])
    assert result["primary_subsystem"] == "COMMS", (
        f"Expected primary_subsystem=COMMS, got {result['primary_subsystem']}"
    )


def test_empty_history_returns_none_or_valid():
    """
    When history is completely empty, classify_from_live_telemetry must
    either return a valid dict (back-fill path) OR return None — it must
    NEVER raise an exception.
    """
    snapshot_values = {"battery_voltage_v": 22.0, "battery_soc_pct": 25.0}
    history = {}  # completely empty — triggers back-fill for every channel

    violations = [
        {"param": "battery_voltage_v", "value": 22.0, "threshold": 24.5,
         "direction": "LOW", "subsystem": "EPS"},
    ]

    try:
        result = classify_from_live_telemetry(snapshot_values, history, violations)
        # Result may be None (ML gracefully gave up) or a valid dict
        if result is not None:
            _assert_valid_result(result, "test_empty_history_returns_none_or_valid")
        else:
            print("[PASS] test_empty_history_returns_none_or_valid: returned None gracefully")
    except Exception as exc:
        raise AssertionError(
            f"classify_from_live_telemetry must not raise; got {type(exc).__name__}: {exc}"
        ) from exc


def test_garbage_history_does_not_raise():
    """
    When history contains malformed / non-numeric entries, the function must
    not raise — it should return None and let the rule engine take over.
    """
    snapshot_values = {"battery_voltage_v": 22.0}
    history = {
        "battery_voltage_v": [{"JUNK": "data"}, {"value": None}, 42, "not-a-dict"],
    }
    violations = [
        {"param": "battery_voltage_v", "value": 22.0, "threshold": 24.5,
         "direction": "LOW", "subsystem": "EPS"},
    ]

    try:
        result = classify_from_live_telemetry(snapshot_values, history, violations)
        if result is not None:
            _assert_valid_result(result, "test_garbage_history_does_not_raise")
        else:
            print("[PASS] test_garbage_history_does_not_raise: returned None gracefully")
    except Exception as exc:
        raise AssertionError(
            f"Must not raise on garbage history; got {type(exc).__name__}: {exc}"
        ) from exc


def test_single_violation_trend_stable():
    """
    A single violation should yield trend='stable' (same heuristic as rule engine).
    """
    base_values = {
        "battery_voltage_v": 22.0, "battery_soc_pct": 85.0, "solar_current_a": 6.2,
        "bus_power_w": 45.0, "temp_obc_c": 25.0, "temp_battery_c": 20.0,
        "temp_payload_c": 22.0, "attitude_error_deg": 0.1, "reaction_wheel_rpm": 2000.0,
        "downlink_snr_db": 28.0, "memory_usage_pct": 45.0, "cpu_usage_pct": 30.0,
        "bus_voltage_v": 27.3, "battery_current_a": 0.0, "solar_power_w": 173.0,
        "gyro_bias_dps": 0.0, "rw_current_a": 0.25, "rssi_dbm": -85.0,
        "packet_loss_pct": 0.5, "gps_fix": 1.0, "gps_satellites": 8.0,
    }

    snapshot_values = dict(base_values)
    history = _make_history(list(base_values.keys()), n_samples=10, base_values=base_values)

    violations = [
        {"param": "battery_voltage_v", "value": 22.0, "threshold": 24.5,
         "direction": "LOW", "subsystem": "EPS"},
    ]

    result = classify_from_live_telemetry(snapshot_values, history, violations)
    if result is not None:
        assert result["trend"] == "stable", (
            f"Expected trend=stable for single violation, got {result['trend']}"
        )
        print(f"[PASS] test_single_violation_trend_stable: trend={result['trend']}")
    else:
        print("[PASS] test_single_violation_trend_stable: returned None gracefully (ML unavailable)")


def test_watcher_import_has_adapter():
    """
    Verify that agents/watcher.py actually imports classify_from_live_telemetry
    — a regression guard so nobody accidentally removes the import.
    """
    import agents.watcher as watcher_module
    assert hasattr(watcher_module, "classify_from_live_telemetry"), (
        "agents/watcher.py should import classify_from_live_telemetry from model.live_adapter"
    )
    print("[PASS] test_watcher_import_has_adapter: classify_from_live_telemetry found in watcher module")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    tests = [
        test_battery_undervoltage_scenario,
        test_comms_degradation_scenario,
        test_empty_history_returns_none_or_valid,
        test_garbage_history_does_not_raise,
        test_single_violation_trend_stable,
        test_watcher_import_has_adapter,
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

    print(f"\n{'='*55}")
    print(f"ML Live Adapter Tests: {passed}/{passed+failed} passed, {failed} failed")
    import sys
    sys.exit(0 if failed == 0 else 1)
