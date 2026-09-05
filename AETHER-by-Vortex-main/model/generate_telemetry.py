"""
Synthetic telemetry generator for LYRA-1 (AETHER / Team Vortex) - v2.

Rebuilt against the CURRENT config.py in FlixonCoder/AETHER-by-Vortex,
which now defines 20 telemetry channels and 10 primary causal anomaly
scenarios (up from the original 5). Baselines, deltas and durations are
pulled directly from TELEMETRY_PARAMS / ANOMALY_SCENARIOS so the synthetic
data matches what the real simulator (telemetry/simulator.py) produces.

Note on the 4 "legacy alias" scenarios in config.py (attitude_drift,
memory_overflow, thermal_excursion, comms_loss): these carry the exact
same deltas as rw_saturation, obc_memory_overflow, battery_overtemperature
and comms_degradation respectively - they're the same physical signature
under an old name for backward compatibility. Training a classifier to
tell two identical signals apart under different labels is meaningless
(and actively hurts accuracy), so this generator trains on the 10 PRIMARY
scenarios only. The inference layer can map a primary prediction back to
its legacy alias name for display if needed.

Channels (20, from TELEMETRY_PARAMS):
  battery_voltage_v, battery_soc_pct, solar_current_a, bus_power_w,
  temp_obc_c, temp_battery_c, temp_payload_c, attitude_error_deg,
  reaction_wheel_rpm, downlink_snr_db, memory_usage_pct, cpu_usage_pct,
  bus_voltage_v, battery_current_a, solar_power_w, gyro_bias_dps,
  rw_current_a, rssi_dbm, packet_loss_pct, gps_fix, gps_satellites
"""
import numpy as np
import pandas as pd

WINDOW_LEN = 40  # samples per episode; covers the 30-35 tick anomaly durations used in config.py

CHANNELS = [
    "battery_voltage_v", "battery_soc_pct", "solar_current_a", "bus_power_w",
    "temp_obc_c", "temp_battery_c", "temp_payload_c", "attitude_error_deg",
    "reaction_wheel_rpm", "downlink_snr_db", "memory_usage_pct", "cpu_usage_pct",
    "bus_voltage_v", "battery_current_a", "solar_power_w", "gyro_bias_dps",
    "rw_current_a", "rssi_dbm", "packet_loss_pct", "gps_fix", "gps_satellites",
]

# nominal baseline, pulled from config.py TELEMETRY_PARAMS "nominal"
NOMINAL_BASELINE = {
    "battery_voltage_v": 28.0, "battery_soc_pct": 85.0, "solar_current_a": 6.2, "bus_power_w": 45.0,
    "temp_obc_c": 25.0, "temp_battery_c": 20.0, "temp_payload_c": 22.0, "attitude_error_deg": 0.1,
    "reaction_wheel_rpm": 2000.0, "downlink_snr_db": 28.0, "memory_usage_pct": 45.0, "cpu_usage_pct": 30.0,
    "bus_voltage_v": 27.3, "battery_current_a": 0.0, "solar_power_w": 173.0, "gyro_bias_dps": 0.0,
    "rw_current_a": 0.25, "rssi_dbm": -85.0, "packet_loss_pct": 0.5, "gps_fix": 1.0, "gps_satellites": 8.0,
}

# noise sized at ~1.2% of each channel's (max-min) range from config.py, so nominal
# telemetry realistically wobbles without crossing warn thresholds
NOMINAL_NOISE = {
    "battery_voltage_v": 0.20, "battery_soc_pct": 1.2, "solar_current_a": 0.10, "bus_power_w": 1.6,
    "temp_obc_c": 0.9, "temp_battery_c": 0.7, "temp_payload_c": 1.1, "attitude_error_deg": 0.03,
    "reaction_wheel_rpm": 60.0, "downlink_snr_db": 0.5, "memory_usage_pct": 1.2, "cpu_usage_pct": 1.2,
    "bus_voltage_v": 0.19, "battery_current_a": 0.15, "solar_power_w": 3.0, "gyro_bias_dps": 0.02,
    "rw_current_a": 0.05, "rssi_dbm": 1.0, "packet_loss_pct": 0.2, "gps_fix": 0.0, "gps_satellites": 0.3,
}

# The 10 primary causal scenarios, deltas pulled directly from config.py ANOMALY_SCENARIOS
ANOMALY_SCENARIOS = {
    "battery_undervoltage": {
        "severity": "LOW", "duration_ticks": 30,
        "deltas": {"battery_voltage_v": -4.8, "battery_soc_pct": -25.0, "bus_voltage_v": -4.5},
    },
    "solar_array_degradation": {
        "severity": "MEDIUM", "duration_ticks": 35,
        "deltas": {"solar_current_a": -4.5, "solar_power_w": -120.0, "battery_soc_pct": -15.0},
    },
    "rw_saturation": {
        "severity": "MEDIUM", "duration_ticks": 30,
        "deltas": {"reaction_wheel_rpm": 3600.0, "attitude_error_deg": 2.8, "rw_current_a": 2.8},
    },
    "gyro_drift": {
        "severity": "MEDIUM", "duration_ticks": 30,
        "deltas": {"gyro_bias_dps": 0.35, "attitude_error_deg": 2.2},
    },
    "battery_overtemperature": {
        "severity": "HIGH", "duration_ticks": 35,
        "deltas": {"temp_battery_c": 23.0, "temp_payload_c": 35.0, "temp_obc_c": 21.0},
    },
    "obc_memory_overflow": {
        "severity": "MEDIUM", "duration_ticks": 35,
        "deltas": {"memory_usage_pct": 44.0, "cpu_usage_pct": 54.0},
    },
    "comms_degradation": {
        "severity": "CRITICAL", "duration_ticks": 30,
        "deltas": {"downlink_snr_db": -21.0, "packet_loss_pct": 35.0, "rssi_dbm": -22.0},
    },
    "gps_loss": {
        "severity": "HIGH", "duration_ticks": 35,
        "deltas": {"gps_fix": -1.0, "gps_satellites": -7.0},
    },
    "power_bus_overcurrent": {
        "severity": "HIGH", "duration_ticks": 30,
        "deltas": {"bus_power_w": 60.0, "battery_current_a": 5.0},
    },
    "solar_thermal_excursion": {
        "severity": "HIGH", "duration_ticks": 35,
        "deltas": {"attitude_error_deg": 2.5, "temp_payload_c": 35.0, "temp_obc_c": 26.0},
    },
}

# maps each legacy alias name (from config.py) to the primary scenario it's
# physically identical to, for display purposes only
LEGACY_ALIASES = {
    "attitude_drift": "rw_saturation",
    "memory_overflow": "obc_memory_overflow",
    "thermal_excursion": "battery_overtemperature",
    "comms_loss": "comms_degradation",
}


def _nominal_series(length, rng):
    return {
        ch: NOMINAL_BASELINE[ch] + rng.normal(0, NOMINAL_NOISE[ch], length)
        for ch in CHANNELS
    }


def _inject_ramp(series, onset, length, target_delta, noise_scale, rng):
    t = np.arange(length)
    ramp = np.clip((t - onset) / max(1, (length - onset) * 0.6), 0, 1) ** 1.5
    series = series + ramp * target_delta
    series = series + rng.normal(0, noise_scale, length)
    return series


def make_episode(label, rng, length=WINDOW_LEN):
    data = _nominal_series(length, rng)

    if label == "nominal":
        onset = -1
    else:
        scenario = ANOMALY_SCENARIOS[label]
        duration = scenario["duration_ticks"]
        onset = int(rng.integers(low=max(1, length - duration), high=max(2, length // 2)))
        for ch, delta in scenario["deltas"].items():
            noise = NOMINAL_NOISE[ch] * 1.5  # slightly noisier during fault onset
            data[ch] = _inject_ramp(data[ch], onset, length, delta, noise, rng)

    df = pd.DataFrame(data)
    df["t"] = np.arange(length)
    df["label"] = label
    df["onset"] = onset
    return df


def build_dataset(n_per_class=250, seed=42):
    rng = np.random.default_rng(seed)
    labels = ["nominal"] + list(ANOMALY_SCENARIOS.keys())
    episodes = []
    episode_id = 0
    for label in labels:
        n = n_per_class * 2 if label == "nominal" else n_per_class
        for _ in range(n):
            ep = make_episode(label, rng)
            ep["episode_id"] = episode_id
            episodes.append(ep)
            episode_id += 1
    return pd.concat(episodes, ignore_index=True)


if __name__ == "__main__":
    df = build_dataset(n_per_class=250)
    df.to_csv("data/lyra1_telemetry_raw_v2.csv", index=False)
    print(f"Generated {df['episode_id'].nunique()} episodes, {len(df)} rows, {len(CHANNELS)} channels")
    print(df.groupby("label")["episode_id"].nunique())
