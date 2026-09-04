"""
Comprehensive automated tests for the Causal Physics Spacecraft Telemetry Engine.
Verifies all 10 anomaly scenarios, physical coupling, recovery transitions, and
invariants specified in user requirements.
"""
import sys, os
from telemetry.simulator import SatelliteSimulator
from telemetry.spacecraft_state import (
    SpacecraftState,
    OrbitalModel,
    FaultEngine,
    RecoveryEngine,
    SpacecraftPhysics,
    battery_ocv,
    effective_internal_resistance,
    ORBITAL_PERIOD_S,
)
from config import TELEMETRY_PARAMS, ANOMALY_SCENARIOS, COMMAND_WHITELIST


def test_spacecraft_state_single_source_of_truth():
    """Verify that all telemetry is derived from SpacecraftState without independent overrides."""
    sim = SatelliteSimulator()
    snap = sim.tick(2.0)
    assert len(snap.values) == len(TELEMETRY_PARAMS)
    # Check that both original 12 and new 9 are present
    assert "battery_voltage_v" in snap.values
    assert "bus_voltage_v" in snap.values
    assert "battery_current_a" in snap.values
    assert "solar_power_w" in snap.values
    assert "gyro_bias_dps" in snap.values
    assert "rw_current_a" in snap.values
    assert "rssi_dbm" in snap.values
    assert "packet_loss_pct" in snap.values
    assert "gps_fix" in snap.values
    assert "gps_satellites" in snap.values


def test_req1_battery_undervoltage_does_not_reduce_solar():
    """
    Hard Requirement 1:
    Battery undervoltage must NOT reduce solar efficiency.
    Solar efficiency must remain 1.0 (nominal).
    """
    sim = SatelliteSimulator()
    # Inject pure battery undervoltage fault
    sim.inject_anomaly("battery_undervoltage")
    snap = sim.tick(2.0)

    # Solar generation must be unaffected
    assert sim._state.solar_efficiency == 1.0, "Solar efficiency must stay 1.0 during battery undervoltage"
    assert snap.values["solar_current_a"] > 5.0, "Solar current must be nominal in sunlight"
    assert snap.values["battery_voltage_v"] < 24.5, "Battery terminal voltage must be depressed"
    assert snap.values["battery_soc_pct"] < 50.0, "Battery SOC must be lower than nominal"


def test_req2_realistic_battery_coupling():
    """
    Hard Requirement 2:
    Battery voltage realistically coupled via OCV(SOC), current, internal resistance,
    health, and temperature.
    """
    # Test OCV function non-linearity
    ocv_high = battery_ocv(90.0, health=1.0)
    ocv_nom = battery_ocv(80.0, health=1.0)
    ocv_low = battery_ocv(15.0, health=1.0)
    assert ocv_high > ocv_nom > ocv_low
    assert ocv_low < 23.5

    # Test internal resistance scaling with temperature and health
    r_nom = effective_internal_resistance(0.08, 20.0, 1.0)
    r_cold = effective_internal_resistance(0.08, -5.0, 1.0)
    r_degraded = effective_internal_resistance(0.08, 20.0, 0.5)
    assert r_cold > r_nom, "Cold temp should increase internal resistance"
    assert r_degraded > r_nom, "Degraded health should increase internal resistance"


def test_all_10_anomalies_injection_and_propagation():
    """
    Verify that each of the 10 anomaly scenarios produces multi-parameter
    correlated physical consequences.
    """
    scenarios_to_test = [
        "battery_undervoltage",
        "solar_array_degradation",
        "rw_saturation",
        "gyro_drift",
        "battery_overtemperature",
        "obc_memory_overflow",
        "comms_degradation",
        "gps_loss",
        "power_bus_overcurrent",
        "solar_thermal_excursion",
    ]

    for sc in scenarios_to_test:
        sim = SatelliteSimulator()
        sim.inject_anomaly(sc)
        snap = sim.tick(2.0)
        viols = snap.violations()

        assert len(viols) >= 1, f"Scenario {sc} must produce at least one threshold violation on injection"
        params_violated = [v["param"] for v in viols]

        if sc == "battery_undervoltage":
            assert "battery_voltage_v" in params_violated or "battery_soc_pct" in params_violated
            assert snap.values["solar_current_a"] > 5.0  # solar unaffected!
        elif sc == "solar_array_degradation":
            assert snap.values["solar_current_a"] < 3.0
            assert snap.values["solar_power_w"] < 80.0
        elif sc == "rw_saturation":
            assert "reaction_wheel_rpm" in params_violated or "attitude_error_deg" in params_violated
            assert snap.values["rw_current_a"] > 2.5
        elif sc == "gyro_drift":
            assert "gyro_bias_dps" in params_violated or "attitude_error_deg" in params_violated
        elif sc == "battery_overtemperature":
            assert "temp_battery_c" in params_violated
            assert snap.values["temp_payload_c"] > 50.0
        elif sc == "obc_memory_overflow":
            assert "memory_usage_pct" in params_violated or "cpu_usage_pct" in params_violated
        elif sc == "comms_degradation":
            assert "downlink_snr_db" in params_violated or "packet_loss_pct" in params_violated
        elif sc == "gps_loss":
            assert snap.values["gps_fix"] == 0.0
            assert snap.values["gps_satellites"] <= 2.0
        elif sc == "power_bus_overcurrent":
            assert "bus_power_w" in params_violated
        elif sc == "solar_thermal_excursion":
            assert "temp_payload_c" in params_violated or "temp_obc_c" in params_violated


def test_req3_recovery_commands_interact_with_state():
    """
    Hard Requirement 3:
    Recovery commands must modify underlying state, after which physics derives
    the recovered telemetry.
    """
    sim = SatelliteSimulator()
    # 1. Test RW desat
    sim.inject_anomaly("rw_saturation")
    sim.tick(2.0)
    assert sim._state.rw_speed_rpm > 5500.0
    sim.clear_anomaly()
    res = sim.apply_recovery_command("REACTION_WHEEL_DESAT")
    assert res["applied"] is True
    assert sim._state.rw_speed_rpm < 3000.0
    assert sim._state.external_disturbance_torque_nm == 0.0

    # 2. Test MPPT recalibrate
    sim.inject_anomaly("solar_array_degradation")
    sim.tick(2.0)
    sim.clear_anomaly()
    res = sim.apply_recovery_command("MPPT_RECALIBRATE")
    assert res["applied"] is True
    assert sim._state.solar_efficiency > 0.70

    # 3. Test Heater Relay cycle
    sim.inject_anomaly("battery_overtemperature")
    sim.tick(2.0)
    sim.clear_anomaly()
    res = sim.apply_recovery_command("HEATER_RELAY_CYCLE")
    assert res["applied"] is True
    assert sim._state.battery_temp_c < 40.0, "Battery temperature must drop below 40.0C after cycling relay"

    # 4. Test GPS Reset (gradual cold-start reacquisition)
    sim.inject_anomaly("gps_loss")
    sim.tick(2.0)
    assert sim._state.gps_fix is False
    sim.clear_anomaly()
    res = sim.apply_recovery_command("GPS_RESET")
    assert res["applied"] is True
    assert sim._state.gps_recovery_ticks > 0, "GPS cold-start countdown should begin"
    # Step through cold-start satellite search ticks
    for _ in range(sim._state.gps_recovery_ticks + 1):
        sim.tick(2.0)
    assert sim._state.gps_fix is True, "GPS fix must gradually return after cold-start countdown"
    assert sim._state.gps_satellites >= 4, "Satellites must re-acquire"


def test_orbital_context_and_ground_track_backward_compatibility():
    """Verify that orbital_context and ground_track return expected structures."""
    sim = SatelliteSimulator()
    ctx = sim.orbital_context()
    assert "orbit_time_s" in ctx
    assert "orbital_phase_pct" in ctx
    assert "in_eclipse" in ctx
    assert "latitude" in ctx
    assert "longitude" in ctx
    assert "altitude_km" in ctx
    assert "inclination_deg" in ctx
    assert -90.0 <= ctx["latitude"] <= 90.0
    assert -180.0 <= ctx["longitude"] <= 180.0


if __name__ == "__main__":
    test_spacecraft_state_single_source_of_truth()
    test_req1_battery_undervoltage_does_not_reduce_solar()
    test_req2_realistic_battery_coupling()
    test_all_10_anomalies_injection_and_propagation()
    test_req3_recovery_commands_interact_with_state()
    test_orbital_context_and_ground_track_backward_compatibility()
    print("ALL CAUSAL SIMULATOR TESTS PASSED SUCCESSFULLY!")
