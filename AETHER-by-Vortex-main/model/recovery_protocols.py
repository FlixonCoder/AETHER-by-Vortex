"""
Recovery protocol lookup for the 10 primary AETHER anomaly scenarios,
matching the physics described in config.py's ANOMALY_SCENARIOS.
"""
from generate_telemetry import LEGACY_ALIASES

RECOVERY_PROTOCOLS = {
    "battery_undervoltage": {
        "severity": "LOW",
        "actions": [
            "Shed non-essential loads to reduce bus draw",
            "Increase charge duty cycle during sunlit orbit segments",
            "Flag affected battery cell string for capacity derating",
        ],
    },
    "solar_array_degradation": {
        "severity": "MEDIUM",
        "actions": [
            "Re-point solar arrays for optimal incidence angle",
            "Reduce non-critical loads to match reduced generation capacity",
            "Log array string for ground-team degradation trending",
        ],
    },
    "rw_saturation": {
        "severity": "MEDIUM",
        "actions": [
            "Perform magnetorquer-based momentum dumping",
            "Command reaction wheel desaturation maneuver",
            "Temporarily suspend fine-pointing payload operations",
        ],
    },
    "gyro_drift": {
        "severity": "MEDIUM",
        "actions": [
            "Trigger star-tracker aided gyro bias re-estimation",
            "Cross-check attitude solution against sun sensor / GPS",
            "Increase attitude filter update rate temporarily",
        ],
    },
    "battery_overtemperature": {
        "severity": "HIGH",
        "actions": [
            "Disable battery heater circuit (suspected contact weld / stuck-on fault)",
            "Increase radiator exposure / reorient for passive cooling",
            "Reduce charge current until temperature returns to nominal band",
        ],
    },
    "obc_memory_overflow": {
        "severity": "MEDIUM",
        "actions": [
            "Flush/downlink buffered payload data",
            "Restart the runaway process/buffer owner",
            "Clear non-critical cache and reduce logging verbosity",
        ],
    },
    "comms_degradation": {
        "severity": "CRITICAL",
        "actions": [
            "Command antenna gimbal re-home / unbind sequence",
            "Switch to omni-directional backup antenna",
            "Step down to a lower, more robust data rate mode",
            "Enter beacon mode to aid ground re-acquisition if link doesn't recover",
        ],
    },
    "gps_loss": {
        "severity": "HIGH",
        "actions": [
            "Power-cycle GPS receiver front end",
            "Fall back to propagated orbit solution from last good fix",
            "Cross-check with ground-station ranging if available",
        ],
    },
    "power_bus_overcurrent": {
        "severity": "HIGH",
        "actions": [
            "Trip/isolate the overcurrent payload branch",
            "Verify battery temperature after excess current draw",
            "Re-enable branch only after fault isolation confirms clear",
        ],
    },
    "solar_thermal_excursion": {
        "severity": "HIGH",
        "actions": [
            "Correct attitude pointing error driving excess solar absorption",
            "Reduce duty cycle of heat-generating subsystems in affected bay",
            "Monitor payload/OBC temperature for continued rise post-correction",
        ],
    },
    "nominal": {
        "severity": "NOMINAL",
        "actions": ["No action required - continue nominal monitoring"],
    },
}


def get_recovery_protocol(anomaly_type: str) -> dict:
    # resolve legacy alias names (attitude_drift, memory_overflow, etc.) to their primary scenario
    resolved = LEGACY_ALIASES.get(anomaly_type, anomaly_type)
    return RECOVERY_PROTOCOLS.get(
        resolved,
        {"severity": "UNKNOWN", "actions": ["Anomaly type not recognized - escalate to Mission Commander agent"]},
    )
