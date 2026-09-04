"""
Deterministic Rule-Based Engine (Mode 3 Fallback) for Autonomous Mission Operations.
Provides guaranteed deterministic decisions, anomaly classification, root cause analysis,
candidate procedures, and digital twin simulation outcomes if AI models are unavailable or fail.
"""
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

PARAM_PRIORITY = [
    ("downlink_snr_db",    ("comms_degradation",      "COMMS",   "CRITICAL")),
    ("packet_loss_pct",    ("comms_degradation",      "COMMS",   "CRITICAL")),
    ("rssi_dbm",           ("comms_degradation",      "COMMS",   "HIGH")),
    ("gps_fix",            ("gps_loss",               "ADCS",    "HIGH")),
    ("gps_satellites",     ("gps_loss",               "ADCS",    "HIGH")),
    ("temp_battery_c",     ("battery_overtemperature","THERMAL", "HIGH")),
    ("temp_payload_c",     ("solar_thermal_excursion","THERMAL", "HIGH")),
    ("temp_obc_c",         ("solar_thermal_excursion","THERMAL", "HIGH")),
    ("bus_power_w",        ("power_bus_overcurrent",  "EPS",     "HIGH")),
    ("battery_current_a",  ("power_bus_overcurrent",  "EPS",     "HIGH")),
    ("solar_current_a",    ("solar_array_degradation","EPS",     "MEDIUM")),
    ("solar_power_w",      ("solar_array_degradation","EPS",     "MEDIUM")),
    ("gyro_bias_dps",      ("gyro_drift",             "ADCS",    "MEDIUM")),
    ("reaction_wheel_rpm", ("rw_saturation",          "ADCS",    "MEDIUM")),
    ("rw_current_a",       ("rw_saturation",          "ADCS",    "MEDIUM")),
    ("attitude_error_deg", ("rw_saturation",          "ADCS",    "MEDIUM")),
    ("memory_usage_pct",   ("obc_memory_overflow",    "OBC",     "MEDIUM")),
    ("cpu_usage_pct",      ("obc_memory_overflow",    "OBC",     "MEDIUM")),
    ("battery_voltage_v",  ("battery_undervoltage",   "EPS",     "LOW")),
    ("bus_voltage_v",      ("battery_undervoltage",   "EPS",     "LOW")),
    ("battery_soc_pct",    ("battery_undervoltage",   "EPS",     "LOW")),
]

SCENARIOS = {
    # ── 1. Battery Undervoltage ──
    "battery_undervoltage": {
        "subsystem": "EPS",
        "summary": "Battery terminal voltage and state-of-charge are falling below safe operating thresholds under normal bus load.",
        "root_cause": "Internal battery cell capacity fade and elevated internal resistance causing terminal voltage collapse under normal bus load (solar generation nominal).",
        "hypotheses": [
            {
                "cause": "Battery cell degradation and internal resistance escalation under load",
                "probability": 0.85,
                "evidence": ["Terminal voltage below 24.5V under normal load", "Internal resistance elevated", "Solar efficiency nominal"]
            },
            {
                "cause": "Excessive parasitic payload bus draw",
                "probability": 0.15,
                "evidence": ["Battery discharge rate elevated during normal operations"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-EPS-01",
                "name": "Load Shedding & Non-Essential Isolation",
                "description": "Shed non-essential payload loads to reduce battery discharge rate and stabilize terminal voltage.",
                "commands": [
                    {"command": "LOAD_SHED_NON_ESSENTIAL", "parameters": {"level": "standard"}}
                ],
                "expected_outcome": "Discharge current drops; terminal voltage stabilizes above 24.5V",
                "risk": "LOW",
                "estimated_recovery_probability": 0.94,
                "mission_impact": "Science payload in standby for 1 orbit",
                "reversible": True
            },
            {
                "action_id": "ACT-EPS-02",
                "name": "Enter Safe Mode Hold",
                "description": "Transition spacecraft to safe mode holding sun point to minimize power consumption.",
                "commands": [
                    {"command": "SAFE_MODE_ENTER", "parameters": {}}
                ],
                "expected_outcome": "Minimum bus power state ensures spacecraft survival",
                "risk": "LOW",
                "estimated_recovery_probability": 0.97,
                "mission_impact": "All scientific operations suspended",
                "reversible": True
            }
        ]
    },

    # ── 2. Solar Array Degradation ──
    "solar_array_degradation": {
        "subsystem": "EPS",
        "summary": "Solar array generation current and power have degraded significantly below expected orbital values.",
        "root_cause": "Solar array string open-circuit degradation combined with MPPT tracking curve drift reducing generation below orbit-average load.",
        "hypotheses": [
            {
                "cause": "Solar array string open-circuit degradation with MPPT setpoint drift",
                "probability": 0.89,
                "evidence": ["Solar current > 50% below nominal for sunlit phase", "Battery net discharging during sunlight"]
            },
            {
                "cause": "Off-nominal solar array pointing angle",
                "probability": 0.11,
                "evidence": ["Solar incidence cosine loss"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-EPS-03",
                "name": "MPPT Recalibration & Sun-Tracking Bias",
                "description": "Recalibrate MPPT tracking curve and re-orient solar array vector to optimal sun angle.",
                "commands": [
                    {"command": "MPPT_RECALIBRATE", "parameters": {}},
                    {"command": "ATTITUDE_HOLD_SUN", "parameters": {"bias_deg": 0.0}}
                ],
                "expected_outcome": "Solar array output recovers toward nominal; battery net charging restored",
                "risk": "LOW",
                "estimated_recovery_probability": 0.92,
                "mission_impact": "Payload operations paused for 15 minutes during calibration",
                "reversible": True
            }
        ]
    },

    # ── 3. Reaction Wheel Saturation ──
    "rw_saturation": {
        "subsystem": "ADCS",
        "summary": "Reaction wheel speed and momentum factor are approaching saturation limits.",
        "root_cause": "Persistent external disturbance torque accumulating angular momentum toward reaction wheel saturation.",
        "hypotheses": [
            {
                "cause": "External disturbance torque momentum accumulation driving reaction wheels to saturation",
                "probability": 0.92,
                "evidence": ["Wheel RPM exceeding 5500 RPM threshold", "Attitude error trending > 2.0 degrees", "Motor current elevated"]
            },
            {
                "cause": "Bearing friction increase in reaction wheel assembly",
                "probability": 0.08,
                "evidence": ["Elevated motor drive power"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-ADCS-01",
                "name": "Reaction Wheel Magnetorquer Desaturation",
                "description": "Activate magnetic torquer coils to dump angular momentum to Earth's magnetic field.",
                "commands": [
                    {"command": "REACTION_WHEEL_DESAT", "parameters": {"target_wheel_rpm": 2000.0}}
                ],
                "expected_outcome": "Reaction wheel RPM drops below 2200 RPM; attitude error returns < 0.2 deg",
                "risk": "LOW",
                "estimated_recovery_probability": 0.95,
                "mission_impact": "Minor pointing jitter during 10-minute dump maneuver",
                "reversible": True
            }
        ]
    },

    # ── 4. Gyroscope Drift ──
    "gyro_drift": {
        "subsystem": "ADCS",
        "summary": "Gyroscope rate bias is drifting off-nominal, causing attitude pointing errors.",
        "root_cause": "Temperature-dependent MEMS gyroscope bias drift causing closed-loop attitude estimation error.",
        "hypotheses": [
            {
                "cause": "Temperature-gradient induced gyro bias drift",
                "probability": 0.90,
                "evidence": ["Gyro bias > 0.15 deg/s", "Attitude pointing error growing steadily"]
            },
            {
                "cause": "Star tracker optical blind excursion",
                "probability": 0.10,
                "evidence": ["Attitude reference divergence"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-ADCS-02",
                "name": "In-Flight Gyro Recalibration & Sun Hold",
                "description": "Initiate star-tracker aided in-flight gyroscope bias re-estimation and hold sun reference.",
                "commands": [
                    {"command": "GYRO_RECALIBRATE", "parameters": {}},
                    {"command": "ATTITUDE_HOLD_SUN", "parameters": {"bias_deg": 0.0}}
                ],
                "expected_outcome": "Gyro bias resets near zero; attitude error drops below 0.2 deg",
                "risk": "LOW",
                "estimated_recovery_probability": 0.93,
                "mission_impact": "Brief science payload pause during calibration",
                "reversible": True
            }
        ]
    },

    # ── 5. Battery Overtemperature ──
    "battery_overtemperature": {
        "subsystem": "THERMAL",
        "summary": "Battery-bay and payload temperatures are climbing toward upper limits.",
        "root_cause": "Heater controller solid-state relay has suffered a short-circuit contact weld, continuously powering thermal heater strips in the battery compartment.",
        "hypotheses": [
            {
                "cause": "Battery bay heater control relay welded in CLOSED state",
                "probability": 0.89,
                "evidence": ["Battery temp elevated above 40 deg C", "Heater bus drawing power continuously"]
            },
            {
                "cause": "Thermal insulation degradation or radiator view blockage",
                "probability": 0.11,
                "evidence": ["Slow rate of heat rejection during eclipse"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-THM-01",
                "name": "Heater Relay Forced Cycle & Radiator Slew",
                "description": "Send high-voltage relay cycle pulse and slew spacecraft to cold deep space radiator bias.",
                "commands": [
                    {"command": "HEATER_RELAY_CYCLE", "parameters": {"relay_id": "BAY_1"}},
                    {"command": "RADIATOR_SLEW_BIAS", "parameters": {"bias_angle": 15.0}}
                ],
                "expected_outcome": "Relay contact opens; temperatures stabilize back into nominal 18-24 deg C range",
                "risk": "MEDIUM",
                "estimated_recovery_probability": 0.90,
                "mission_impact": "Off-nadir attitude offset for 20 minutes",
                "reversible": True
            }
        ]
    },

    # ── 6. OBC Memory Overflow ──
    "obc_memory_overflow": {
        "subsystem": "OBC",
        "summary": "On-board computer volatile memory and CPU utilization approaching saturation.",
        "root_cause": "A runaway telemetry buffer allocation routine is leaking pointer memory faster than downlink flushes, saturating kernel heap and starving system tasks.",
        "hypotheses": [
            {
                "cause": "Payload data handler buffer memory leak and heap saturation",
                "probability": 0.93,
                "evidence": ["RAM usage steadily climbing past 85%", "CPU load > 80%"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-OBC-01",
                "name": "Payload Buffer Flush & Task Soft Restart",
                "description": "Commit mission data checkpoint to non-volatile flash and restart payload data handling daemon.",
                "commands": [
                    {"command": "PAYLOAD_BUFFER_FLUSH", "parameters": {"backup_flash": True}},
                    {"command": "OBC_SOFT_RESTART_TASK", "parameters": {"task_name": "payload_handler"}}
                ],
                "expected_outcome": "RAM usage drops below 50%; CPU utilization returns to nominal < 35%",
                "risk": "LOW",
                "estimated_recovery_probability": 0.95,
                "mission_impact": "30-second pause in sensor data ingest",
                "reversible": True
            }
        ]
    },

    # ── 7. Communications Degradation ──
    "comms_degradation": {
        "subsystem": "COMMS",
        "summary": "Downlink signal-to-noise ratio has collapsed below ground station link margin.",
        "root_cause": "Antenna dual-axis pointing gimbal mechanical drive binding off-boresight, causing severe high-gain beam pointing offset.",
        "hypotheses": [
            {
                "cause": "Antenna pointing gimbal mechanical drive jammed off-boresight",
                "probability": 0.88,
                "evidence": ["Downlink SNR dropped > 15 dB", "Packet loss elevated > 20%", "RSSI below -105 dBm"]
            },
            {
                "cause": "Ground station RF interference or atmospheric attenuation",
                "probability": 0.12,
                "evidence": ["Contact angle low on horizon"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-COM-01",
                "name": "Emergency UHF Failover & Antenna Rehoming",
                "description": "Switch telemetry to omni-directional UHF backup link and execute motor rehome cycle.",
                "commands": [
                    {"command": "COMMS_UHF_FAILOVER", "parameters": {}},
                    {"command": "ANTENNA_GIMBAL_REHOME", "parameters": {"axis": "all"}}
                ],
                "expected_outcome": "UHF link verified immediately; S-band high-gain restored after rehoming",
                "risk": "MEDIUM",
                "estimated_recovery_probability": 0.88,
                "mission_impact": "High-rate science downlink suspended until rehome completes",
                "reversible": True
            }
        ]
    },

    # ── 8. GPS / Navigation Loss ──
    "gps_loss": {
        "subsystem": "ADCS",
        "summary": "GPS receiver has lost carrier lock and valid navigation fix.",
        "root_cause": "GPS receiver front-end tracking loop lock loss or firmware state stall leading to loss of orbit determination.",
        "hypotheses": [
            {
                "cause": "GPS receiver RF front-end tracking loss or firmware stall",
                "probability": 0.91,
                "evidence": ["GPS fix flag false (0)", "Tracked satellites < 3", "Navigation solution degraded"]
            },
            {
                "cause": "Solar radio burst or ionospheric scintillation",
                "probability": 0.09,
                "evidence": ["Space weather event in progress"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-NAV-01",
                "name": "GPS Receiver Cold-Start Reset",
                "description": "Issue cold-start receiver reset command and hold sun-pointing reference while satellites reacquire.",
                "commands": [
                    {"command": "GPS_RESET", "parameters": {}},
                    {"command": "ATTITUDE_HOLD_SUN", "parameters": {"bias_deg": 0.0}}
                ],
                "expected_outcome": "Receiver reinitializes almanac and recovers 6+ satellite locks within 45s",
                "risk": "LOW",
                "estimated_recovery_probability": 0.94,
                "mission_impact": "Autonomous orbit determination offline for 1 minute",
                "reversible": True
            }
        ]
    },

    # ── 9. Power Bus Overcurrent ──
    "power_bus_overcurrent": {
        "subsystem": "EPS",
        "summary": "Main power bus load is significantly elevated, driving heavy battery discharge.",
        "root_cause": "Secondary payload branch low-impedance short-circuit causing power bus overcurrent and accelerated battery heating.",
        "hypotheses": [
            {
                "cause": "Secondary payload branch latch-up or short-circuit overcurrent",
                "probability": 0.92,
                "evidence": ["Bus power draw > 100W", "Battery discharge current > 4.5A", "Battery pack warming"]
            },
            {
                "cause": "Thermal control heater simultaneous firing anomaly",
                "probability": 0.08,
                "evidence": ["Thermal sub-bus current spike"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-EPS-04",
                "name": "Bus Overcurrent Branch Isolation & Load Shed",
                "description": "Command solid-state power distribution switch to isolate shorted payload branch and shed non-essential loads.",
                "commands": [
                    {"command": "BUS_OVERCURRENT_ISOLATE", "parameters": {}},
                    {"command": "LOAD_SHED_NON_ESSENTIAL", "parameters": {"level": "standard"}}
                ],
                "expected_outcome": "Faulted branch isolated; bus power drops to nominal < 48W; battery discharge stops",
                "risk": "MEDIUM",
                "estimated_recovery_probability": 0.95,
                "mission_impact": "Faulted payload instrument powered down until diagnostic downlink",
                "reversible": True
            }
        ]
    },

    # ── 10. Solar Array / Attitude Thermal Excursion ──
    "solar_thermal_excursion": {
        "subsystem": "THERMAL",
        "summary": "Payload and OBC temperatures are rising due to off-nominal solar pointing.",
        "root_cause": "Attitude pointing excursion increasing solar flux absorption on sensitive radiator and payload bays.",
        "hypotheses": [
            {
                "cause": "Off-nominal solar pointing attitude increasing direct solar absorption",
                "probability": 0.88,
                "evidence": ["Attitude error > 2.0 deg", "Payload temp > 55 deg C", "OBC temp > 50 deg C"]
            },
            {
                "cause": "Radiator contamination or degradation",
                "probability": 0.12,
                "evidence": ["Thermal emissivity change"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-THM-02",
                "name": "Sun-Pointing Restoral & Radiator Slew",
                "description": "Realign spacecraft attitude to nominal sun-pointing axis and slew radiator to deep space.",
                "commands": [
                    {"command": "ATTITUDE_HOLD_SUN", "parameters": {"bias_deg": 0.0}},
                    {"command": "RADIATOR_SLEW_BIAS", "parameters": {"bias_angle": 15.0}}
                ],
                "expected_outcome": "Thermal absorption drops; temperatures stabilize back into nominal envelope",
                "risk": "LOW",
                "estimated_recovery_probability": 0.93,
                "mission_impact": "Science payload observing paused during slew maneuver",
                "reversible": True
            }
        ]
    },

    # ── Legacy Aliases (Full Backward Compatibility) ──
    "attitude_drift": {
        "subsystem": "ADCS",
        "summary": "Reaction wheel speed and attitude pointing error are drifting beyond nominal bounds.",
        "root_cause": "External disturbance torque accumulating momentum toward reaction wheel saturation.",
        "hypotheses": [
            {
                "cause": "External disturbance torque momentum accumulation",
                "probability": 0.91,
                "evidence": ["Wheel RPM exceeding 5000 RPM", "Attitude error > 2.0 degrees"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-ADCS-01",
                "name": "Reaction Wheel Magnetorquer Desaturation",
                "description": "Activate magnetic torquer coils to dump angular momentum to Earth's magnetic field.",
                "commands": [
                    {"command": "REACTION_WHEEL_DESAT", "parameters": {"target_wheel_rpm": 2000.0}}
                ],
                "expected_outcome": "Reaction wheel RPM decreases below 2200 RPM; attitude error returns < 0.3 deg",
                "risk": "LOW",
                "estimated_recovery_probability": 0.94,
                "mission_impact": "Minor pointing jitter during dump maneuver",
                "reversible": True
            }
        ]
    },
    "thermal_excursion": {
        "subsystem": "THERMAL",
        "summary": "Battery-bay and payload temperatures are climbing toward upper limits.",
        "root_cause": "Heater controller solid-state relay has suffered a short-circuit contact weld, continuously powering thermal heater strips in the battery compartment.",
        "hypotheses": [
            {
                "cause": "Battery bay heater control relay welded in CLOSED state",
                "probability": 0.89,
                "evidence": ["Battery temp elevated above 35 deg C", "Heater bus drawing power continuously in sunlit phase"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-THM-01",
                "name": "Heater Relay Forced Cycle & Radiator Slew",
                "description": "Send high-voltage relay cycle pulse and slew spacecraft to cold deep space radiator bias.",
                "commands": [
                    {"command": "HEATER_RELAY_CYCLE", "parameters": {"relay_id": "BAY_1"}},
                    {"command": "RADIATOR_SLEW_BIAS", "parameters": {"bias_angle": 15.0}}
                ],
                "expected_outcome": "Relay contact opens; temperatures stabilize back into nominal 18-24 deg C range",
                "risk": "MEDIUM",
                "estimated_recovery_probability": 0.90,
                "mission_impact": "Off-nadir attitude offset for 20 minutes",
                "reversible": True
            }
        ]
    },
    "memory_overflow": {
        "subsystem": "OBC",
        "summary": "On-board computer volatile memory and CPU utilization approaching saturation.",
        "root_cause": "A runaway telemetry buffer allocation routine is leaking pointer memory faster than downlink flushes, saturating kernel heap and starving system tasks.",
        "hypotheses": [
            {
                "cause": "Payload data handler buffer memory leak",
                "probability": 0.93,
                "evidence": ["RAM usage steadily climbing past 85%", "Task scheduling delay increasing"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-OBC-01",
                "name": "Payload Buffer Flush & Task Soft Restart",
                "description": "Commit mission data checkpoint to non-volatile flash and restart payload data handling daemon.",
                "commands": [
                    {"command": "PAYLOAD_BUFFER_FLUSH", "parameters": {"backup_flash": True}},
                    {"command": "OBC_SOFT_RESTART_TASK", "parameters": {"task_name": "payload_handler"}}
                ],
                "expected_outcome": "RAM usage drops below 50%; CPU utilization returns to nominal < 35%",
                "risk": "LOW",
                "estimated_recovery_probability": 0.95,
                "mission_impact": "30-second pause in sensor data ingest",
                "reversible": True
            }
        ]
    },
    "comms_loss": {
        "subsystem": "COMMS",
        "summary": "Downlink signal-to-noise ratio has collapsed below ground station link margin.",
        "root_cause": "Antenna dual-axis pointing gimbal mechanical drive binding off-boresight, causing severe high-gain beam pointing offset.",
        "hypotheses": [
            {
                "cause": "Antenna pointing gimbal mechanical drive jammed off-boresight",
                "probability": 0.87,
                "evidence": ["Downlink SNR dropped by 22 dB", "Gimbal drive position feedback mismatch"]
            }
        ],
        "candidates": [
            {
                "action_id": "ACT-COM-01",
                "name": "Emergency UHF Failover & Antenna Rehoming",
                "description": "Switch telemetry to omni-directional UHF backup link and execute motor rehome cycle.",
                "commands": [
                    {"command": "COMMS_UHF_FAILOVER", "parameters": {}},
                    {"command": "ANTENNA_GIMBAL_REHOME", "parameters": {"axis": "all"}}
                ],
                "expected_outcome": "UHF link verified immediately; S-band high-gain restored after rehoming",
                "risk": "MEDIUM",
                "estimated_recovery_probability": 0.86,
                "mission_impact": "High-rate science downlink suspended until rehome completes",
                "reversible": True
            }
        ]
    }
}

GENERIC_SCENARIO = {
    "subsystem": "OBC",
    "summary": "Operational parameter threshold violation detected.",
    "root_cause": "Anomalous subsystem boundary crossing requiring state stabilization.",
    "hypotheses": [
        {
            "cause": "Unmodeled environmental disturbance or component state drift",
            "probability": 0.75,
            "evidence": ["Parameter excursion outside nominal threshold envelope"]
        }
    ],
    "candidates": [
        {
            "action_id": "ACT-GEN-01",
            "name": "Stabilization & Safe Mode Hold",
            "description": "Isolate secondary loads and hold stable sun-pointing configuration.",
            "commands": [
                {"command": "SAFE_MODE_ENTER", "parameters": {}}
            ],
            "expected_outcome": "Spacecraft safely stabilized",
            "risk": "LOW",
            "estimated_recovery_probability": 0.95,
            "mission_impact": "Temporary operational pause",
            "reversible": True
        }
    ]
}


class DeterministicRuleEngine:
    """Provides instant, infallible deterministic decisions for satellite operations."""

    def resolve_scenario(self, anomaly_type: str, subsystem: str, param_names: Optional[List[str]] = None) -> dict:
        if anomaly_type in SCENARIOS:
            return SCENARIOS[anomaly_type]
        if param_names:
            for p in param_names:
                for target_p, mapping in PARAM_PRIORITY:
                    if p == target_p:
                        return SCENARIOS.get(mapping[0], GENERIC_SCENARIO)
        for sc in SCENARIOS.values():
            if sc["subsystem"].upper() == subsystem.upper():
                return sc
        return GENERIC_SCENARIO

    def watcher_classify(self, violations: List[dict], current_values: dict) -> dict:
        """Classify anomaly deterministically from violation parameters."""
        params = [v["param"] for v in violations if "param" in v]
        anomaly_type, subsystem, default_sev = ("threshold_violation", "OBC", "MEDIUM")

        for param in params:
            for target_p, mapping in PARAM_PRIORITY:
                if param == target_p:
                    anomaly_type, subsystem, default_sev = mapping
                    break
            if anomaly_type != "threshold_violation":
                break

        if subsystem == "OBC" and violations and "subsystem" in violations[0]:
            subsystem = violations[0]["subsystem"]

        sc = self.resolve_scenario(anomaly_type, subsystem, params)
        confidence = 0.92 if len(violations) >= 2 else 0.80

        return {
            "anomaly_type": anomaly_type,
            "primary_subsystem": subsystem,
            "severity": default_sev,
            "affected_params": params or [violations[0]["param"]],
            "trend": "worsening" if len(violations) >= 2 else "stable",
            "confidence": confidence,
            "summary": sc["summary"]
        }

    def identify(self, anomaly: dict, history: dict, rag_context: List[dict]) -> dict:
        """Deterministic root-cause identification with multi-hypothesis generation."""
        ano_type = anomaly.get("anomaly_type", "")
        subsystem = anomaly.get("primary_subsystem", "OBC")
        sc = self.resolve_scenario(ano_type, subsystem, anomaly.get("affected_params", []))

        hypotheses = list(sc["hypotheses"])
        # Inject lessons from RAG if available
        if rag_context:
            for r in rag_context:
                if r.get("similarity_score", 0) > 0.3:
                    hypotheses.append({
                        "cause": f"Historical recurring failure: {r.get('root_cause', '')[:100]}",
                        "probability": round(float(r.get("similarity_score", 0.5)) * 0.8, 2),
                        "evidence": [f"Matches historical incident {r.get('incident_id', '')}"]
                    })

        return {
            "root_cause": sc["root_cause"],
            "confidence": 0.91,
            "hypotheses": hypotheses,
            "affected_subsystem": subsystem,
            "reasoning": f"Deterministic diagnostic mapping identified signature matching {ano_type} in {subsystem} subsystem."
        }

    def find_fixes(self, anomaly: dict, diagnosis: dict, rag_procedures: List[dict]) -> List[dict]:
        """Deterministic recovery candidates matching whitelist commands."""
        ano_type = anomaly.get("anomaly_type", "")
        subsystem = anomaly.get("primary_subsystem", "OBC")
        sc = self.resolve_scenario(ano_type, subsystem, anomaly.get("affected_params", []))
        candidates = [dict(c) for c in sc["candidates"]]

        # If RAG provided validated procedures, prefer and prioritize them
        if rag_procedures:
            for proc in rag_procedures:
                if proc.get("subsystem", "").upper() == subsystem.upper():
                    candidates.insert(0, {
                        "action_id": proc.get("procedure_id", "ACT-RAG-01"),
                        "name": proc.get("name", "Verified Procedure"),
                        "description": proc.get("description", "Historical procedural recovery"),
                        "commands": proc.get("commands", []),
                        "expected_outcome": proc.get("expected_outcome", "Restore nominal telemetry"),
                        "risk": proc.get("risk", "LOW"),
                        "estimated_recovery_probability": proc.get("success_rate", 0.92),
                        "mission_impact": "Operational stabilization",
                        "reversible": proc.get("reversible", True)
                    })
                    break

        return candidates

    def simulate(self, candidate: dict, current_telemetry: dict) -> dict:
        """Deterministic forward physics check for candidate procedure."""
        from config import COMMAND_WHITELIST, TELEMETRY_PARAMS
        deltas = {}
        for cmd in candidate.get("commands", []):
            cmd_name = cmd.get("command")
            if cmd_name in COMMAND_WHITELIST:
                for k, v in COMMAND_WHITELIST[cmd_name].get("deltas", {}).items():
                    deltas[k] = deltas.get(k, 0.0) + v

        predicted = {}
        for k, v in current_telemetry.items():
            meta = TELEMETRY_PARAMS.get(k)
            new_val = v + deltas.get(k, 0.0)
            if meta:
                new_val = max(meta["min"], min(meta["max"], new_val))
            predicted[k] = round(new_val, 3)

        # Physics coupling: bus voltage follows battery voltage
        if "battery_voltage_v" in predicted:
            predicted["bus_voltage_v"] = round(predicted["battery_voltage_v"] * 0.975, 3)

        constraints_passed = True
        constraint_results = []

        if predicted.get("battery_soc_pct", 100) < 25.0:
            constraints_passed = False
            constraint_results.append("CRITICAL: Battery SOC predicted < 25%")
        else:
            constraint_results.append("PASS: Battery SOC in safe margin")

        if predicted.get("battery_voltage_v", 28.0) < 24.0:
            constraints_passed = False
            constraint_results.append("CRITICAL: Bus voltage predicted < 24V")
        else:
            constraint_results.append("PASS: Bus voltage in safe margin")

        risk_score = 15 if constraints_passed and candidate.get("risk") == "LOW" else (40 if constraints_passed else 85)

        return {
            "action_id": candidate.get("action_id", "ACT-01"),
            "safe": constraints_passed,
            "predicted_state": predicted,
            "constraint_results": constraint_results,
            "recovery_probability": candidate.get("estimated_recovery_probability", 0.90) if constraints_passed else 0.20,
            "risk_score": risk_score,
            "reason": "Forward physics state trajectory cleared safe" if constraints_passed else "Constraint violation in predicted trajectory"
        }
