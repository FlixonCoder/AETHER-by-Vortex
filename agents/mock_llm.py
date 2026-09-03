"""
Offline mock LLM engine.
Handles fallback scenario synthesis transparently across the response pipeline.
"""
import json
from datetime import datetime, timezone

class _TextBlock:
    def __init__(self, text: str):
        self.text = text

class _Response:
    def __init__(self, text: str):
        self.content = [_TextBlock(text)]

class _Messages:
    def __init__(self, outer):
        self._outer = outer

    def create(self, model=None, max_tokens=None, messages=None, **kwargs) -> _Response:
        if messages:
            content = messages[0].get("content", "")
            prompt = content if isinstance(content, str) else json.dumps(content)
            return _Response(self._outer.respond(prompt))

class MockAnthropic:
    def __init__(self, *args, **kwargs):
        self.messages = _Messages(self)

    def respond(self, prompt: str) -> str:
        if "telemetry monitor agent" in prompt:
            return self._monitor(prompt)
        if "root-cause analysis" in prompt:
            return self._diagnose(prompt)
        if "Generate recovery procedures" in prompt:
            return self._recovery(prompt)
        if "digital-twin validation" in prompt:
            return self._twin(prompt)
        if "operator runbook" in prompt:
            return self._runbook(prompt)
        return "{}"

    def _monitor(self, prompt: str) -> str:
        violations = _extract_after("THRESHOLD VIOLATIONS:", prompt) or []
        params = [v.get("param") for v in violations if isinstance(v, dict)]
        
        anomaly_type, subsystem, severity = ("threshold_violation", "OBC", "MEDIUM")
        for key, mapping in PARAM_PRIORITY:
            if key in params:
                anomaly_type, subsystem, severity = mapping
                break
                
        if subsystem == "OBC" and violations and isinstance(violations[0], dict):
            subsystem = violations[0].get("subsystem", subsystem)
            
        sc = _scenario_for_type(anomaly_type, subsystem)
        trend = "worsening" if len(violations) >= 2 else "stable"
        confidence = 0.9 if len(violations) >= 2 else 0.75
        
        return json.dumps({
            "severity": severity,
            "primary_subsystem": subsystem,
            "anomaly_type": anomaly_type,
            "affected_params": params or [anomaly_type],
            "trend": trend,
            "confidence": confidence,
            "summary": sc["summary"]
        })

    def _diagnose(self, prompt: str) -> str:
        anomaly = _extract_after("ANOMALY REPORT:", prompt) or {}
        anomaly_type = anomaly.get("anomaly_type", "")
        subsystem = anomaly.get("primary_subsystem", "OBC")
        sc = _scenario_for_type(anomaly_type, subsystem)
        
        return json.dumps({
            "root_cause": sc["root_cause"],
            "confidence": 0.87,
            "contributing_factors": sc["contributing_factors"],
            "affected_components": sc["affected_components"],
            "risk_assessment": sc["risk_assessment"],
            "recommended_actions": sc["recommended_actions"],
            "similar_known_anomalies": sc["similar_known_anomalies"],
            "reasoning_chain": sc["reasoning_chain"]
        })

    def _recovery(self, prompt: str) -> str:
        anomaly = _extract_after("ANOMALY:", prompt) or {}
        anomaly_type = anomaly.get("anomaly_type", "")
        subsystem = anomaly.get("primary_subsystem", "OBC")
        sc = _scenario_for_type(anomaly_type, subsystem)
        
        return json.dumps({
            "options": sc["options"],
            "recommended_option_rank": 1,
            "planning_notes": (
                f"Options ranked by combined effectiveness, safety and reversibility for the "
                f"{subsystem} {anomaly_type or 'anomaly'}. Rank 1 is recommended; escalate to lower "
                f"ranks only if it proves insufficient."
            )
        })

    def _twin(self, prompt: str) -> str:
        option = _extract_after("PROCEDURE:", prompt) or {}
        sim = _extract_after("SIMULATION RESULTS:", prompt) or {}
        
        viable = bool(sim.get("simulation_viable", True))
        violations = sim.get("constraint_violations", []) or []
        success_prob = option.get("success_probability", 0.8)
        flags = sim.get("simulation_flags", []) or []
        
        hard_violation = any("SOC below 35%" in v for v in violations)
        
        if viable and not violations:
            status = "APPROVED"
            effectiveness = int(min(95, round(success_prob * 100)))
            go = True
        elif flags and not hard_violation:
            status = "CONDITIONAL"
            effectiveness = int(max(50, round(success_prob * 100 * 0.85)))
            go = True
        else:
            status = "REJECTED"
            effectiveness = 20
            go = False
            
        residual = list(violations)
        if not residual and status != "APPROVED":
            residual = ["Monitor post-recovery telemetry for regression"]
            
        notes = f"Forward simulation applied {len(flags)} recovery effect(s) ({', '.join(flags) if flags else 'no state-changing effects detected'}). "
        if go:
            notes += "Predicted post-procedure state satisfies mission constraints - cleared to execute with standard monitoring."
        else:
            notes += "Predicted state violates one or more constraints - do not execute without revision."
            
        return json.dumps({
            "validation_status": status,
            "predicted_effectiveness_pct": effectiveness,
            "residual_risks": residual or ["None significant beyond routine monitoring"],
            "unexpected_side_effects": [] if viable else ["Constraint violation in predicted state"],
            "operator_notes": notes,
            "go_no_go": go
        })

    def _runbook(self, prompt: str) -> str:
        anomaly = _extract_after("ANOMALY SUMMARY:", prompt) or {}
        diagnosis = _extract_after("ANOMALY DIAGNOSIS:", prompt) or {}
        proc = _extract_after("APPROVED RECOVERY PROCEDURE:", prompt) or {}
        
        ano_id = anomaly.get("id", "ANO-UNKNOWN")
        severity = anomaly.get("severity", "-")
        subsystem = anomaly.get("primary_subsystem", "-")
        anomaly_type = anomaly.get("anomaly_type", "anomaly")
        title = anomaly_type.replace("_", " ").title()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        
        root_cause = diagnosis.get("root_cause", "See diagnostics report.")
        risk = diagnosis.get("risk_assessment", {}) or {}
        steps = proc.get("steps", []) or []
        proc_name = proc.get("name", "Approved Procedure")
        val = proc.get("validation", {}) or {}
        sim = proc.get("simulation", {}) or {}
        success = proc.get("success_probability", 0)
        duration = proc.get("estimated_duration_min", "-")
        
        def tag_step(i, s):
            low = s.lower()
            if any(k in low for k in ["verify", "confirm", "check"]):
                t = "VER"
            elif any(k in low for k in ["monitor", "watch", "trend", "await", "observe"]):
                t = "MON"
            else:
                t = "CMD"
            return f"{i}. [{t}] {s}"

        exec_block = "\n".join(tag_step(i, s) for i, s in enumerate(steps, 1)) or "1. [CMD] Execute approved procedure."
        contributing_md = "\n".join(f"- {c}" for c in (diagnosis.get("contributing_factors", []) or [])) or "- See diagnostics report."
        watch_md = "\n".join(f"- `{p}` - verify it holds within nominal band" for p in list((sim.get("predicted_values", {}) or {}).keys())[:5]) or "- Monitor all parameters."

        return f"""# RUNBOOK: {title}
**ID:** {ano_id}  **Severity:** {severity}  **Subsystem:** {subsystem}  
**Generated:** {now}  

## 1. Situation Summary
{anomaly.get('summary', 'Anomaly detected requiring operator response.')}

## 2. Root Cause
{root_cause}

**Immediate risk:** {risk.get('immediate_risk', '-')}  
**Mission impact:** {risk.get('mission_impact', '-')}  
**Time to critical:** {risk.get('time_to_critical', '-')}  

## 3. Pre-Execution Checklist
- [ ] Confirm current telemetry matches the reported anomaly signature
- [ ] Verify ground station contact window is available
- [ ] Confirm battery SOC ≥ 35% before any maneuver
- [ ] Confirm science data is backed up
- [ ] Verify no conflicting command sequence is active
- [ ] Review contingency actions (Section 6) before starting
- [ ] Log operator identity and authorisation for **{proc_name}**

## 4. Execution Procedure - {proc_name}
*Estimated duration: {duration} min • Predicted success: {round(float(success) * 100) if isinstance(success, (int, float)) else success}% • Validation: {val.get('validation_status', '-')}*

---
{exec_block}

## 5. Success Criteria
- Affected parameters return to and hold within their nominal bands
- Digital-twin predicted effectiveness: {val.get('predicted_effectiveness_pct', '-')}%
- {val.get('operator_notes', 'Confirm nominal operation before closing the anomaly.')}

## 6. Contingency Actions
- Halt the procedure and safe the affected subsystem
- Fall back to the next-ranked recovery option
- Escalate to ground for manual intervention at the next pass

## 7. Post-Recovery Monitoring
{watch_md}

## 8. Lessons Learned / Root-Cause Notes
{contributing_md}
"""

def _parse_leading_json(s: str):
    s = s.lstrip()
    if not s or s[0] not in "[{": return None
    open_ch = s[0]
    close_ch = "]" if open_ch == "[" else "}"
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(s):
        if esc: { esc := False }; continue
        if ch == "\\": { esc := True }; continue
        if ch == '"': { in_str := not in_str }; continue
        if in_str: continue
        if ch == open_ch: depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try: return json.loads(s[:i + 1])
                except json.JSONDecodeError: return None
    return None

def _extract_after(label: str, prompt: str):
    idx = prompt.find(label)
    if idx == -1: return None
    return _parse_leading_json(prompt[idx + len(label):])

def _scenario_for_type(anomaly_type: str, subsystem: str) -> dict:
    if anomaly_type in SCENARIOS: return SCENARIOS[anomaly_type]
    for sc in SCENARIOS.values():
        if sc["subsystem"] == subsystem: return sc
    return GENERIC

PARAM_PRIORITY = [
    ("downlink_snr_db",    ("comms_loss",            "COMMS",   "CRITICAL")),
    ("temp_payload_c",     ("thermal_excursion",     "THERMAL", "HIGH")),
    ("temp_battery_c",     ("thermal_excursion",     "THERMAL", "HIGH")),
    ("temp_obc_c",         ("thermal_excursion",     "THERMAL", "HIGH")),
    ("battery_voltage_v",  ("battery_undervoltage",  "EPS",     "HIGH")),
    ("battery_soc_pct",    ("battery_undervoltage",  "EPS",     "HIGH")),
    ("solar_current_a",    ("battery_undervoltage",  "EPS",     "HIGH")),
    ("attitude_error_deg", ("attitude_drift",        "ADCS",    "MEDIUM")),
    ("reaction_wheel_rpm", ("attitude_drift",        "ADCS",    "MEDIUM")),
    ("memory_usage_pct",   ("memory_overflow",       "OBC",     "MEDIUM")),
    ("cpu_usage_pct",      ("memory_overflow",       "OBC",     "MEDIUM"))
]

SCENARIOS = {
    "battery_undervoltage": {
        "subsystem": "EPS",
        "summary": "Battery bus voltage and state-of-charge are falling below safe operating thresholds.",
        "root_cause": "Progressive solar-array string degradation combined with an MPPT charge-controller set-point drift is reducing array output below the orbit-average load. The battery is discharging faster than it recharges during insolation, pulling bus voltage and SOC below their warning limits.",
        "contributing_factors": [
            "Solar wing 2 output ~30% below nominal (suspected cell string open-circuit)",
            "MPPT tracking set-point drift after last firmware patch",
            "Elevated eclipse-phase load from payload heater"
        ],
        "affected_components": ["Solar array wing 2", "MPPT charge controller", "Li-Ion battery pack", "PDU"],
        "risk_assessment": {"immediate_risk": "High - sustained discharge risks OBC under-voltage lockout", "mission_impact": "Payload operations suspended; safe-mode entry risk", "time_to_critical": "~2 orbits"},
        "recommended_actions": ["Shed non-essential loads", "Reconfigure MPPT parameters"],
        "similar_known_anomalies": ["LYRA-1 2024-DOY-112"],
        "reasoning_chain": ["Voltage and SOC declining together points to resource path ingestion loops."],
        "options": [
            {
                "rank": 1, "name": "Load Shed & Charge Recovery",
                "approach": "Shed loads and optimize tracking.",
                "steps": [
                    "Command payload to standby to disable non-essential loads",
                    "Reconfigure MPPT charge controller to conservative set-point",
                    "Bias attitude for maximum solar incidence during next insolation",
                    "Verify battery charge current turns positive",
                    "Monitor bus voltage recovery for one full orbit"
                ],
                "estimated_duration_min": 12, "success_probability": 0.88, "risk_level": "LOW", "reversible": True,
                "side_effects": ["Science collection paused"], "constraints_checked": ["Battery SOC >= 35%: PASS"],
                "rationale": "Directly targets extraction imbalance variables."
            },
            {
                "rank": 2, "name": "Safe-Mode Power Hold",
                "approach": "Safe mode entry context hold configurations.",
                "steps": ["Disable non-essential loads and enter safe mode", "Hold sun-pointing attitude"],
                "estimated_duration_min": 6, "success_probability": 0.95, "risk_level": "LOW", "reversible": True,
                "side_effects": ["All operations suspended"], "constraints_checked": ["Safe-mode entry criteria: PASS"],
                "rationale": "Maximises platform protection structures explicitly."
            }
        ]
    },
    "thermal_excursion": {
        "subsystem": "THERMAL",
        "summary": "Battery-bay and payload temperatures are climbing toward their upper safety limits.",
        "root_cause": "A heater controller relay has failed closed, driving continuous heating in the battery bay. With radiators unable to reject the excess load during insolation, battery and payload temperatures are trending toward thermal-runaway limits.",
        "contributing_factors": ["Heater relay stuck-closed", "Insolation rejection constraints"],
        "affected_components": ["Battery heater relay", "Radiator panels"],
        "risk_assessment": {"immediate_risk": "High - battery bay approaching upper ceiling limits", "mission_impact": "Cell degradation risk", "time_to_critical": "~1 orbit"},
        "recommended_actions": ["Cycle heater relay parameters", "Slew to radiator exposure profile"],
        "similar_known_anomalies": ["THM-03 operational matrix logs"],
        "reasoning_chain": ["Multi-point anomalies indicate persistent power convergence signatures."],
        "options": [
            {
                "rank": 1, "name": "Heater Relay Cycle & Radiator Bias",
                "approach": "Inhibit hardware schedule paths dynamically.",
                "steps": [
                    "Command heater controller to force-off and cycle the affected relay",
                    "Slew to radiator-favourable attitude to improve heat rejection",
                    "Reduce payload duty cycle to lower internal dissipation",
                    "Verify battery temperature trend reverses within 2 samples",
                    "Restore nominal heater schedule once temperatures stabilise"
                ],
                "estimated_duration_min": 10, "success_probability": 0.86, "risk_level": "LOW", "reversible": True,
                "side_effects": ["Throughput dip"], "constraints_checked": ["Thermal bands: PASS"],
                "rationale": "Directly isolates failed conduction pathways gracefully."
            }
        ]
    },
    "attitude_drift": {
        "subsystem": "ADCS",
        "summary": "Attitude error and reaction-wheel speed are drifting above nominal control bounds.",
        "root_cause": "Increasing bearing friction in a reaction wheel is forcing the control loop to spin the wheel toward saturation to hold pointing, driving both attitude error and wheel RPM upward.",
        "contributing_factors": ["Reaction wheel bearing wear", "Momentum storage drift anomalies"],
        "affected_components": ["Reaction wheel assembly RWA-2", "Magnetorquers"],
        "risk_assessment": {"immediate_risk": "Medium - tracking degradation", "mission_impact": "Imaging quality limits", "time_to_critical": "~4 orbits"},
        "recommended_actions": ["Command magnetorquer desaturation loops"],
        "similar_known_anomalies": ["ADCS failure logs index B-99"],
        "reasoning_chain": ["Friction spikes increase current drawing limits along the axis."],
        "options": [
            {
                "rank": 1, "name": "Reaction-Wheel Desaturation",
                "approach": "Desaturate wheels using magnetic torquer dumps.",
                "steps": [
                    "Command magnetorquer desaturation of the reaction wheel",
                    "Execute a bounded attitude maneuver to re-null pointing error",
                    "Verify wheel RPM returns toward nominal band",
                    "Confirm attitude error drops below 1.5°",
                    "Resume nominal pointing mode"
                ],
                "estimated_duration_min": 14, "success_probability": 0.90, "risk_level": "LOW", "reversible": True,
                "side_effects": ["Maneuver blackout"], "constraints_checked": ["RPM ceiling criteria: PASS"],
                "rationale": "Standard orbital maintenance mechanics toolsets."
            }
        ]
    },
    "memory_overflow": {
        "subsystem": "OBC",
        "summary": "On-board computer memory and CPU utilisation are climbing toward saturation.",
        "root_cause": "A runaway payload data buffer is leaking memory faster than it is flushed to flash, steadily consuming RAM and driving CPU utilisation up as garbage-collection and paging overhead grows.",
        "contributing_factors": ["Data pointer allocation lockups", "Downlink constraints accumulation"],
        "affected_components": ["OBC RAM structural stacks", "Allocation registers"],
        "risk_assessment": {"immediate_risk": "Medium - response latency escalation", "mission_impact": "Watchdog reset exposure risks", "time_to_critical": "~3 orbits"},
        "recommended_actions": ["Isolate task routines", "Clear target allocation sectors safely"],
        "similar_known_anomalies": ["OBC kernel panic trace logs 2024"],
        "reasoning_chain": ["Leaking memory vectors cause excessive scheduler garbage loops."],
        "options": [
            {
                "rank": 1, "name": "Data-Task Soft Restart",
                "approach": "Flush workspace structures and isolate allocation pipelines.",
                "steps": [
                    "Back up buffered science data to flash before any wipe",
                    "Flush the runaway payload data buffer",
                    "Soft-restart (reboot) the payload data-handling task",
                    "Verify memory and CPU utilisation fall back to nominal",
                    "Resume nominal payload buffering"
                ],
                "estimated_duration_min": 9, "success_probability": 0.88, "risk_level": "LOW", "reversible": True,
                "side_effects": ["Task runtime pauses"], "constraints_checked": ["Sector sync routines: PASS"],
                "rationale": "Reclaims workspace memory registers cleanly without causing complete bus drops."
            },
            {
                "rank": 2, "name": "OBC Watchdog-Safe Reboot",
                "approach": "Full OBC reboot with payload powered off to clear all corrupted state.",
                "steps": [
                    "Back up critical state and science data",
                    "Power off payload before OBC restart",
                    "Command watchdog-safe OBC reboot",
                    "Verify subsystems re-acquire and memory is nominal"
                ],
                "estimated_duration_min": 16, "success_probability": 0.82, "risk_level": "MEDIUM", "reversible": True,
                "side_effects": ["Command blackouts"], "constraints_checked": ["State serialization: PASS"],
                "rationale": "Clears deeper persistent lockups cleanly."
            },
            {
                "rank": 3, "name": "Throttle & Downlink Drain",
                "approach": "Throttle capture cycles and priority dump schedules.",
                "steps": ["Reduce payload capture rate", "Prioritise downlink execution routines"],
                "estimated_duration_min": 30, "success_probability": 0.68, "risk_level": "LOW", "reversible": True,
                "side_effects": ["Throughput attenuation"], "constraints_checked": ["None required"],
                "rationale": "Non-invasive passive clearing strategy layer."
            }
        ]
    },
    "comms_loss": {
        "subsystem": "COMMS",
        "summary": "Downlink signal-to-noise has collapsed below the link-margin threshold.",
        "root_cause": "The S-band antenna pointing mechanism has jammed off-boresight, collapsing the downlink link margin. With the high-gain antenna mispointed, SNR has fallen below the minimum required for reliable downlink.",
        "contributing_factors": ["Gimbal drive mechanical binding state faults"],
        "affected_components": ["S-Band assembly paths", "Gimbal actuators"],
        "risk_assessment": {"immediate_risk": "Critical - telemetry link drops", "mission_impact": "Downlink pipeline structural failures", "time_to_critical": "Immediate"},
        "recommended_actions": ["UHF path fallbacks", "Re-home drive tracking coordinates"],
        "similar_known_anomalies": ["S-Band lock failure parameters index 1A"],
        "reasoning_chain": ["Boresight offset profiles cut connection gains dramatically."],
        "options": [
            {
                "rank": 1, "name": "Antenna Re-home & Repoint",
                "approach": "Fallback loop execution to recover directional target alignments.",
                "steps": [
                    "Fail over to UHF omni emergency link for commanding",
                    "Command S-band antenna gimbal re-home cycle",
                    "Re-point antenna to ground station and bias attitude to assist pointing",
                    "Verify downlink SNR recovers above the 12 dB link-margin threshold",
                    "Restore high-rate downlink and clear UHF fallback"
                ],
                "estimated_duration_min": 13, "success_probability": 0.83, "risk_level": "MEDIUM", "reversible": True,
                "side_effects": ["S-Band pause loops"], "constraints_checked": ["UHF connectivity matches: PASS"],
                "rationale": "Maintains out-of-band monitoring paths using backup omni frameworks."
            }
        ]
    }
}

GENERIC = {
    "subsystem": "OBC", "summary": "Violation observed.", "root_cause": "Investigation incomplete.",
    "contributing_factors": ["Data context small"], "affected_components": ["System core"],
    "risk_assessment": {"immediate_risk": "Medium", "mission_impact": "Degrading metrics profiles", "time_to_critical": "Unknown"},
    "recommended_actions": ["Monitor trends safely"], "similar_known_anomalies": [], "reasoning_chain": ["Boundaries cross bounds."],
    "options": [
        {
            "rank": 1, "name": "Safe-Mode Hold", "approach": "Enter safe mode states to lock boundaries.",
            "steps": ["Disable non-essential loads and enter safe mode", "Hold stable configuration setups"],
            "estimated_duration_min": 6, "success_probability": 0.9, "risk_level": "LOW", "reversible": True,
            "side_effects": ["Suspension of operations"], "constraints_checked": ["Standard profiles: PASS"],
            "rationale": "Safest entry profile point."
        }
    ]
}