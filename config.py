import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()

# --------------------------------------------------------------------------- #
# Offline / demo mode
# --------------------------------------------------------------------------- #
# The system runs fully offline (no Anthropic API key required) using a
# scenario-aware mock LLM. Offline mode is enabled automatically when no
# valid API key is present, or forced with SATOPS_OFFLINE=1.
# A "valid" key is any non-empty value that isn't an obvious placeholder.
_FORCE_OFFLINE = os.getenv("SATOPS_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")
_HAS_VALID_KEY = bool(ANTHROPIC_API_KEY) and ANTHROPIC_API_KEY != "your_api_key_here"
OFFLINE_MODE = _FORCE_OFFLINE or not _HAS_VALID_KEY

# --------------------------------------------------------------------------- #
# LLM provider routing
# --------------------------------------------------------------------------- #
# Keys starting "sk-ant-" go straight to Anthropic. Keys starting "sk-or-"
# are OpenRouter keys and get routed through OpenRouter's OpenAI-compatible
# API instead (OpenRouter proxies Anthropic models under "anthropic/..."
# slugs). Override with LLM_PROVIDER=anthropic|openrouter if auto-detection
# guesses wrong.
_AUTO_PROVIDER = "openrouter" if ANTHROPIC_API_KEY.startswith("sk-or-") else "anthropic"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", _AUTO_PROVIDER).strip().lower()

OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")

# Reasoning models (e.g. NVIDIA Nemotron) emit a long chain-of-thought preamble
# and often hit max_tokens before producing the JSON the agents parse. Setting
# LLM_REASONING_EFFORT=none suppresses that preamble. Empty = don't send the field.
LLM_REASONING_EFFORT = os.getenv("LLM_REASONING_EFFORT", "").strip()

MONITOR_MODEL = "claude-haiku-4-5-20251001"
REASONING_MODEL = "claude-sonnet-5"

# OpenRouter model slugs for the two models above. Override via env if
# OpenRouter's catalog names them differently.
OPENROUTER_MODEL_MAP = {
    MONITOR_MODEL: os.getenv("OPENROUTER_MONITOR_MODEL", "anthropic/claude-haiku-4.5"),
    REASONING_MODEL: os.getenv("OPENROUTER_REASONING_MODEL", "anthropic/claude-sonnet-5"),
}

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
    "battery_voltage_v":  {"unit": "V",    "nominal": 28.0, "min": 22.0,  "max": 33.6,  "warn_low": 24.5,  "warn_high": 32.5,  "subsystem": "EPS"},
    "battery_soc_pct":    {"unit": "%",    "nominal": 85.0, "min": 20.0,  "max": 100.0, "warn_low": 30.0,  "warn_high": 98.0,  "subsystem": "EPS"},
    "solar_current_a":    {"unit": "A",    "nominal": 6.2,  "min": 0.0,   "max": 8.5,   "warn_low": None,  "warn_high": 8.2,   "subsystem": "EPS"},
    "bus_power_w":        {"unit": "W",    "nominal": 45.0, "min": 10.0,  "max": 120.0, "warn_low": 15.0,  "warn_high": 100.0, "subsystem": "EPS"},
    "temp_obc_c":         {"unit": "°C",   "nominal": 25.0, "min": -10.0, "max": 60.0,  "warn_low": -5.0,  "warn_high": 50.0,  "subsystem": "OBC"},
    "temp_battery_c":     {"unit": "°C",   "nominal": 20.0, "min": -5.0,  "max": 45.0,  "warn_low": 0.0,   "warn_high": 40.0,  "subsystem": "EPS"},
    "temp_payload_c":     {"unit": "°C",   "nominal": 22.0, "min": -20.0, "max": 70.0,  "warn_low": -10.0, "warn_high": 55.0,  "subsystem": "THERMAL"},
    "attitude_error_deg": {"unit": "°",    "nominal": 0.1,  "min": 0.0,   "max": 10.0,  "warn_low": None,  "warn_high": 2.0,   "subsystem": "ADCS"},
    "reaction_wheel_rpm": {"unit": "RPM",  "nominal": 2000, "min": -6000, "max": 6000,  "warn_low": -5500, "warn_high": 5500,  "subsystem": "ADCS"},
    "downlink_snr_db":    {"unit": "dB",   "nominal": 28.0, "min": 0.0,   "max": 45.0,  "warn_low": 12.0,  "warn_high": None,  "subsystem": "COMMS"},
    "memory_usage_pct":   {"unit": "%",    "nominal": 45.0, "min": 0.0,   "max": 100.0, "warn_low": None,  "warn_high": 85.0,  "subsystem": "OBC"},
    "cpu_usage_pct":      {"unit": "%",    "nominal": 30.0, "min": 0.0,   "max": 100.0, "warn_low": None,  "warn_high": 80.0,  "subsystem": "OBC"},
}

SEVERITY_ORDER = ["NOMINAL", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
AUTO_APPROVE_MAX_SEVERITY = "MEDIUM"  # HIGH and CRITICAL require human approval

TELEMETRY_INTERVAL_S = 2
ANOMALY_CHECK_EVERY_N_TICKS = 4

ANOMALY_SCENARIOS = {
    "battery_undervoltage": {
        "subsystem": "EPS",
        "severity": "HIGH",
        "deltas": {"battery_voltage_v": -6.5, "battery_soc_pct": -35.0, "solar_current_a": -4.0},
        "description": "Solar panel efficiency degradation causing battery undervoltage and insufficient charging",
        "duration_ticks": 40,
    },
    "thermal_excursion": {
        "subsystem": "THERMAL",
        "severity": "HIGH",
        "deltas": {"temp_battery_c": 23.0, "temp_payload_c": 36.0, "temp_obc_c": 20.0},
        "description": "Heater controller malfunction causing thermal runaway in battery bay",
        "duration_ticks": 35,
    },
    "attitude_drift": {
        "subsystem": "ADCS",
        "severity": "MEDIUM",
        "deltas": {"attitude_error_deg": 4.8, "reaction_wheel_rpm": 3100.0},
        "description": "Reaction wheel friction increase causing attitude control degradation",
        "duration_ticks": 30,
    },
    "memory_overflow": {
        "subsystem": "OBC",
        "severity": "MEDIUM",
        "deltas": {"memory_usage_pct": 48.0, "cpu_usage_pct": 52.0},
        "description": "Runaway payload data buffer causing memory exhaustion and CPU saturation",
        "duration_ticks": 45,
    },
    "comms_loss": {
        "subsystem": "COMMS",
        "severity": "CRITICAL",
        "deltas": {"downlink_snr_db": -20.0},
        "description": "Antenna pointing mechanism failure causing link margin violation",
        "duration_ticks": 25,
    },
}