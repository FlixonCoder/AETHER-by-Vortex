import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).parent.resolve()
MEMORY_DIR = BASE_DIR / "memory"
AUDIT_DIR = BASE_DIR / "audit"
RUNBOOK_DIR = BASE_DIR / "runbooks"
ML_MODEL_DIR = BASE_DIR / "model" / "models"

for _d in (MEMORY_DIR, AUDIT_DIR, RUNBOOK_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- #
# 3-Tier AI / LLM Configuration
# Tier 1: Local Ollama (Qwen 0.5B default)
# Tier 2: Cloud Groq (Llama 3.3 / Llama 3)
# Tier 3: Deterministic Rule-Based Engine
# --------------------------------------------------------------------------- #
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:4b").strip()
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60.0"))

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip()
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
GROQ_TIMEOUT = float(os.getenv("GROQ_TIMEOUT", "20.0"))

# Force rule engine flag or offline override
FORCE_RULE_ENGINE = os.getenv("FORCE_RULE_ENGINE", "").strip().lower() in ("1", "true", "yes", "on")
SATOPS_OFFLINE = os.getenv("SATOPS_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")

# Legacy compatibility alias
OFFLINE_MODE = FORCE_RULE_ENGINE or SATOPS_OFFLINE

# --------------------------------------------------------------------------- #
# Subsystems & Telemetry Parameters
# --------------------------------------------------------------------------- #
SUBSYSTEMS = {
    "EPS": "Electrical Power System",
    "ADCS": "Attitude Determination & Control System",
    "COMMS": "Communications Subsystem",
    "THERMAL": "Thermal Control System",
    "OBC": "On-Board Computer",
    "PROPULSION": "Propulsion System",
    "PAYLOAD": "Mission Payload",
}

TELEMETRY_PARAMS = {
    # ── Original 12 parameters (preserved keys & bounds) ──
    "battery_voltage_v":  {"unit": "V",    "nominal": 28.0, "min": 18.0,  "max": 34.0,  "warn_low": 24.5,  "warn_high": 32.5,  "subsystem": "EPS"},
    "battery_soc_pct":    {"unit": "%",    "nominal": 85.0, "min": 0.0,   "max": 100.0, "warn_low": 30.0,  "warn_high": 98.0,  "subsystem": "EPS"},
    "solar_current_a":    {"unit": "A",    "nominal": 6.2,  "min": 0.0,   "max": 8.5,   "warn_low": None,  "warn_high": 8.2,   "subsystem": "EPS"},
    "bus_power_w":        {"unit": "W",    "nominal": 45.0, "min": 10.0,  "max": 140.0, "warn_low": 15.0,  "warn_high": 100.0, "subsystem": "EPS"},
    "temp_obc_c":         {"unit": "°C",   "nominal": 25.0, "min": -10.0, "max": 65.0,  "warn_low": -5.0,  "warn_high": 50.0,  "subsystem": "OBC"},
    "temp_battery_c":     {"unit": "°C",   "nominal": 20.0, "min": -5.0,  "max": 55.0,  "warn_low": 0.0,   "warn_high": 40.0,  "subsystem": "EPS"},
    "temp_payload_c":     {"unit": "°C",   "nominal": 22.0, "min": -20.0, "max": 75.0,  "warn_low": -10.0, "warn_high": 55.0,  "subsystem": "THERMAL"},
    "attitude_error_deg": {"unit": "°",    "nominal": 0.1,  "min": 0.0,   "max": 10.0,  "warn_low": None,  "warn_high": 2.0,   "subsystem": "ADCS"},
    "reaction_wheel_rpm": {"unit": "RPM",  "nominal": 2000, "min": -6000, "max": 6000,  "warn_low": -5500, "warn_high": 5500,  "subsystem": "ADCS"},
    "downlink_snr_db":    {"unit": "dB",   "nominal": 28.0, "min": 0.0,   "max": 45.0,  "warn_low": 12.0,  "warn_high": None,  "subsystem": "COMMS"},
    "memory_usage_pct":   {"unit": "%",    "nominal": 45.0, "min": 0.0,   "max": 100.0, "warn_low": None,  "warn_high": 85.0,  "subsystem": "OBC"},
    "cpu_usage_pct":      {"unit": "%",    "nominal": 30.0, "min": 0.0,   "max": 100.0, "warn_low": None,  "warn_high": 80.0,  "subsystem": "OBC"},
    # ── 9 Causal Physics parameters ──
    "bus_voltage_v":      {"unit": "V",    "nominal": 27.3, "min": 18.0,  "max": 34.0,  "warn_low": 21.0,  "warn_high": 32.0,  "subsystem": "EPS"},
    "battery_current_a":  {"unit": "A",    "nominal": 0.0,  "min": -8.0,  "max": 8.0,   "warn_low": None,  "warn_high": 5.0,   "subsystem": "EPS"},
    "solar_power_w":      {"unit": "W",    "nominal": 173.0,"min": 0.0,   "max": 250.0, "warn_low": None,  "warn_high": None,  "subsystem": "EPS"},
    "gyro_bias_dps":      {"unit": "°/s",  "nominal": 0.0,  "min": -5.0,  "max": 5.0,   "warn_low": -0.15, "warn_high": 0.15,  "subsystem": "ADCS"},
    "rw_current_a":       {"unit": "A",    "nominal": 0.25, "min": 0.0,   "max": 4.5,   "warn_low": None,  "warn_high": 2.8,   "subsystem": "ADCS"},
    "rssi_dbm":           {"unit": "dBm",  "nominal": -85.0,"min": -130.0,"max": -50.0, "warn_low": -105.0,"warn_high": None,  "subsystem": "COMMS"},
    "packet_loss_pct":    {"unit": "%",    "nominal": 0.5,  "min": 0.0,   "max": 100.0, "warn_low": None,  "warn_high": 5.0,   "subsystem": "COMMS"},
    "gps_fix":            {"unit": "bool", "nominal": 1.0,  "min": 0.0,   "max": 1.0,   "warn_low": 0.5,   "warn_high": None,  "subsystem": "ADCS"},
    "gps_satellites":     {"unit": "count","nominal": 8.0,  "min": 0.0,   "max": 16.0,  "warn_low": 4.0,   "warn_high": None,  "subsystem": "ADCS"},
}

SEVERITY_ORDER = ["NOMINAL", "LOW", "MEDIUM", "HIGH", "CRITICAL"]

# Criticality thresholds:
# 0-39: LOW (AUTO APPROVED)
# 40-69: MEDIUM (AUTO APPROVED)
# 70-89: HIGH (HUMAN OVERSIGHT)
# 90-100: CRITICAL (HUMAN APPROVAL REQUIRED)
CRITICALITY_THRESHOLDS = {
    "LOW": (0, 39),
    "MEDIUM": (40, 69),
    "HIGH": (70, 89),
    "CRITICAL": (90, 100)
}

CRITICALITY_POLICIES = {
    "LOW": "AUTO_APPROVED",
    "MEDIUM": "AUTO_APPROVED",
    "HIGH": "HUMAN_OVERSIGHT",
    "CRITICAL": "HUMAN_APPROVAL_REQUIRED"
}

AUTO_APPROVE_MAX_SEVERITY = "MEDIUM"

TELEMETRY_INTERVAL_S = 2
ANOMALY_CHECK_EVERY_N_TICKS = 3
MAX_RECOVERY_ATTEMPTS = 2

# --------------------------------------------------------------------------- #
# Rolling Telemetry Analysis & Statistical Anomaly Filter
# --------------------------------------------------------------------------- #
ROLLING_WINDOW_SECONDS = float(os.getenv("ROLLING_WINDOW_SECONDS", "60.0"))
ROLLING_MIN_SAMPLES = int(os.getenv("ROLLING_MIN_SAMPLES", "2"))
SPIKE_ZSCORE_THRESHOLD = float(os.getenv("SPIKE_ZSCORE_THRESHOLD", "2.5"))
PERSISTENCE_TICKS_THRESHOLD = int(os.getenv("PERSISTENCE_TICKS_THRESHOLD", "3"))
CRITICAL_ZSCORE_THRESHOLD = float(os.getenv("CRITICAL_ZSCORE_THRESHOLD", "4.5"))
EMERGENCY_DEVIATION_RATIO = float(os.getenv("EMERGENCY_DEVIATION_RATIO", "0.35"))

# --------------------------------------------------------------------------- #
# Command Whitelist for Spacecraft Execution
# The LLM CANNOT generate or execute arbitrary commands outside this schema.
# --------------------------------------------------------------------------- #
COMMAND_WHITELIST = {
    "REACTION_WHEEL_DESAT": {
        "subsystem": "ADCS",
        "description": "Desaturate reaction wheels using magnetorquers",
        "reversible": True,
        "deltas": {"reaction_wheel_rpm": -800.0, "attitude_error_deg": -1.5, "rw_current_a": -1.2}
    },
    "ATTITUDE_HOLD_SUN": {
        "subsystem": "ADCS",
        "description": "Orient solar panels toward Sun vector",
        "reversible": True,
        "deltas": {"solar_current_a": 1.8, "solar_power_w": 50.0, "attitude_error_deg": -0.8}
    },
    "LOAD_SHED_NON_ESSENTIAL": {
        "subsystem": "EPS",
        "description": "Turn off non-essential payload heaters and secondary instruments",
        "reversible": True,
        "deltas": {"bus_power_w": -20.0, "battery_soc_pct": 5.0, "battery_voltage_v": 1.0}
    },
    "MPPT_RECALIBRATE": {
        "subsystem": "EPS",
        "description": "Reset maximum power point tracking calibration curve",
        "reversible": True,
        "deltas": {"solar_current_a": 3.5, "solar_power_w": 95.0, "battery_voltage_v": 3.0, "battery_soc_pct": 15.0}
    },
    "HEATER_RELAY_CYCLE": {
        "subsystem": "THERMAL",
        "description": "Force-cycle stuck thermal heater relay to clear contact weld",
        "reversible": True,
        "deltas": {"temp_battery_c": -12.0, "temp_payload_c": -15.0, "temp_obc_c": -8.0}
    },
    "RADIATOR_SLEW_BIAS": {
        "subsystem": "THERMAL",
        "description": "Slew spacecraft to maximize deep-space radiator view factor",
        "reversible": True,
        "deltas": {"temp_battery_c": -8.0, "temp_payload_c": -10.0}
    },
    "PAYLOAD_BUFFER_FLUSH": {
        "subsystem": "OBC",
        "description": "Safely back up science data and flush runaway payload data buffer",
        "reversible": True,
        "deltas": {"memory_usage_pct": -35.0, "cpu_usage_pct": -30.0}
    },
    "OBC_SOFT_RESTART_TASK": {
        "subsystem": "OBC",
        "description": "Restart payload telemetry handling task gracefully",
        "reversible": True,
        "deltas": {"cpu_usage_pct": -15.0, "memory_usage_pct": -10.0}
    },
    "ANTENNA_GIMBAL_REHOME": {
        "subsystem": "COMMS",
        "description": "Rehome antenna pointing gimbal to restore nominal boresight",
        "reversible": True,
        "deltas": {"downlink_snr_db": 15.0, "packet_loss_pct": -25.0, "rssi_dbm": 15.0}
    },
    "COMMS_UHF_FAILOVER": {
        "subsystem": "COMMS",
        "description": "Switch commanding and telemetry to omni UHF backup link",
        "reversible": True,
        "deltas": {"downlink_snr_db": 6.0, "packet_loss_pct": -15.0}
    },
    "SAFE_MODE_ENTER": {
        "subsystem": "OBC",
        "description": "Place satellite into power-positive, Earth-safe orientation",
        "reversible": True,
        "deltas": {"bus_power_w": -30.0, "cpu_usage_pct": -20.0, "battery_soc_pct": 8.0}
    },
    "GPS_RESET": {
        "subsystem": "ADCS",
        "description": "Initiate cold-start receiver reset and satellite reacquisition sequence",
        "reversible": True,
        "deltas": {"gps_fix": 1.0, "gps_satellites": 6.0}
    },
    "BUS_OVERCURRENT_ISOLATE": {
        "subsystem": "EPS",
        "description": "Command solid-state power distribution switch to isolate shorted payload branch",
        "reversible": True,
        "deltas": {"bus_power_w": -55.0, "battery_current_a": -4.0, "battery_voltage_v": 1.5}
    },
    "GYRO_RECALIBRATE": {
        "subsystem": "ADCS",
        "description": "Initiate star-tracker aided in-flight gyroscope bias re-estimation",
        "reversible": True,
        "deltas": {"attitude_error_deg": -1.8, "gyro_bias_dps": -0.30}
    },
    "RF_POWER_BOOST": {
        "subsystem": "COMMS",
        "description": "Boost S-band solid-state power amplifier to maximum transmit level",
        "reversible": True,
        "deltas": {"downlink_snr_db": 5.0, "rssi_dbm": 5.0, "bus_power_w": 8.0}
    }
}

# --------------------------------------------------------------------------- #
# Anomaly Scenarios (10 Primary Physics Scenarios + Legacy Aliases)
# --------------------------------------------------------------------------- #
ANOMALY_SCENARIOS = {
    # ── 10 Primary Causal Anomaly Scenarios ──
    "battery_undervoltage": {
        "subsystem": "EPS",
        "severity": "LOW",
        "deltas": {"battery_voltage_v": -4.8, "battery_soc_pct": -25.0, "bus_voltage_v": -4.5},
        "description": "Aged battery with internal cell degradation causing terminal undervoltage under bus load (solar unaffected)",
        "duration_ticks": 30,
    },
    "solar_array_degradation": {
        "subsystem": "EPS",
        "severity": "MEDIUM",
        "deltas": {"solar_current_a": -4.5, "solar_power_w": -120.0, "battery_soc_pct": -15.0},
        "description": "Solar cell string open-circuit degradation reducing array power below orbit-average load",
        "duration_ticks": 35,
    },
    "rw_saturation": {
        "subsystem": "ADCS",
        "severity": "MEDIUM",
        "deltas": {"reaction_wheel_rpm": 3600.0, "attitude_error_deg": 2.8, "rw_current_a": 2.8},
        "description": "External disturbance torque accumulating angular momentum toward reaction wheel saturation",
        "duration_ticks": 30,
    },
    "gyro_drift": {
        "subsystem": "ADCS",
        "severity": "MEDIUM",
        "deltas": {"gyro_bias_dps": 0.35, "attitude_error_deg": 2.2},
        "description": "Temperature-dependent gyroscope bias drift inducing attitude estimation and pointing error",
        "duration_ticks": 30,
    },
    "battery_overtemperature": {
        "subsystem": "THERMAL",
        "severity": "HIGH",
        "deltas": {"temp_battery_c": 23.0, "temp_payload_c": 35.0, "temp_obc_c": 21.0},
        "description": "Heater controller contact weld continuously powering battery thermal maintenance heaters",
        "duration_ticks": 35,
    },
    "obc_memory_overflow": {
        "subsystem": "OBC",
        "severity": "MEDIUM",
        "deltas": {"memory_usage_pct": 44.0, "cpu_usage_pct": 54.0},
        "description": "Runaway payload data buffer leaking pointer memory and saturating OBC heap/CPU",
        "duration_ticks": 35,
    },
    "comms_degradation": {
        "subsystem": "COMMS",
        "severity": "CRITICAL",
        "deltas": {"downlink_snr_db": -21.0, "packet_loss_pct": 35.0, "rssi_dbm": -22.0},
        "description": "Antenna pointing gimbal mechanical binding causing link margin collapse and packet loss",
        "duration_ticks": 30,
    },
    "gps_loss": {
        "subsystem": "ADCS",
        "severity": "HIGH",
        "deltas": {"gps_fix": -1.0, "gps_satellites": -7.0},
        "description": "GPS receiver frontend loss of lock resulting in loss of orbit determination fix",
        "duration_ticks": 35,
    },
    "power_bus_overcurrent": {
        "subsystem": "EPS",
        "severity": "HIGH",
        "deltas": {"bus_power_w": 60.0, "battery_current_a": 5.0},
        "description": "Payload branch overcurrent fault drawing excessive bus power and heating battery",
        "duration_ticks": 30,
    },
    "solar_thermal_excursion": {
        "subsystem": "THERMAL",
        "severity": "HIGH",
        "deltas": {"attitude_error_deg": 2.5, "temp_payload_c": 35.0, "temp_obc_c": 26.0},
        "description": "Attitude pointing excursion increasing solar radiation absorption on sensitive bays",
        "duration_ticks": 35,
    },
    # ── Legacy Scenario Aliases (Full Backward Compatibility) ──
    "attitude_drift": {
        "subsystem": "ADCS",
        "severity": "MEDIUM",
        "deltas": {"attitude_error_deg": 2.8, "reaction_wheel_rpm": 3600.0, "rw_current_a": 2.8},
        "description": "Reaction wheel friction increase causing attitude control degradation (rw_saturation)",
        "duration_ticks": 30,
    },
    "memory_overflow": {
        "subsystem": "OBC",
        "severity": "MEDIUM",
        "deltas": {"memory_usage_pct": 44.0, "cpu_usage_pct": 54.0},
        "description": "Runaway payload data buffer causing memory exhaustion (obc_memory_overflow)",
        "duration_ticks": 35,
    },
    "thermal_excursion": {
        "subsystem": "THERMAL",
        "severity": "HIGH",
        "deltas": {"temp_battery_c": 23.0, "temp_payload_c": 35.0, "temp_obc_c": 21.0},
        "description": "Heater controller malfunction causing thermal runaway (battery_overtemperature)",
        "duration_ticks": 35,
    },
    "comms_loss": {
        "subsystem": "COMMS",
        "severity": "CRITICAL",
        "deltas": {"downlink_snr_db": -21.0, "packet_loss_pct": 35.0, "rssi_dbm": -22.0},
        "description": "Antenna pointing mechanism failure causing severe link margin violation (comms_degradation)",
        "duration_ticks": 30,
    },
    "ollama_failure_test": {
        "subsystem": "OBC",
        "severity": "HIGH",
        "deltas": {"memory_usage_pct": 44.0, "cpu_usage_pct": 54.0},
        "description": "Demo fault demonstrating Local AI fallback to Cloud (Groq)",
        "duration_ticks": 35,
    },
    "full_offline_test": {
        "subsystem": "ADCS",
        "severity": "CRITICAL",
        "deltas": {"attitude_error_deg": 2.8, "reaction_wheel_rpm": 3600.0, "rw_current_a": 2.8},
        "description": "Demo fault demonstrating LLM failure fallback to Deterministic Rule Engine",
        "duration_ticks": 35,
    }
}