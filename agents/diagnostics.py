"""
Anomaly Diagnostics Agent
"""
import json
import re
from datetime import datetime, timezone
from config import REASONING_MODEL, SUBSYSTEMS, TELEMETRY_PARAMS
from .llm import acreate, get_client

SUBSYSTEM_KNOWLEDGE = {
    "EPS": "Key components: Solar panels, Li-Ion battery pack, MPPT tracking sets, power routing hubs.",
    "ADCS": "Key components: Star trackers, 3-axis reaction wheel assemblies, magnetorquers, IMU modules.",
    "COMMS": "Key components: S-Band patch arrays, UHF emergency transceivers, diplexer interfaces.",
    "THERMAL": "Key components: Multi-layer insulation, active heater pads, radiators, temperature sensors.",
    "OBC": "Key components: ARM processing nodes, NAND flash memory arrays, storage watchdogs."
}

class DiagnosticsAgent:
    def __init__(self):
        self._client = get_client()

    async def diagnose(self, anomaly: dict, snapshot_values: dict, history: dict, orbital_ctx: dict) -> dict:
        subsys = anomaly.get("primary_subsystem", "OBC")
        subsys_kb = SUBSYSTEM_KNOWLEDGE.get(subsys, "No context records.")
        affected = anomaly.get("affected_params", [])
        
        hist_compact = {p: [round(r["value"], 2) for r in history.get(p, [])[-10:]] for p in affected}
        current_affected = {p: {"value": snapshot_values.get(p), "unit": TELEMETRY_PARAMS.get(p, {}).get("unit", "")} for p in affected}

        prompt = f"""You are an expert satellite systems engineer performing root-cause analysis.
ANOMALY REPORT: {json.dumps(anomaly)}
CURRENT VALUES: {json.dumps(current_affected)}
PARAMETER TRENDS: {json.dumps(hist_compact)}
SUBSYSTEM DATA: {subsys_kb}

Return JSON with: root_cause, confidence, contributing_factors, affected_components, risk_assessment, recommended_actions, similar_known_anomalies, reasoning_chain.
"""
        try:
            resp = await acreate(
                self._client,
                model=REASONING_MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            result = json.loads(raw)
        except Exception as e:
            print(f"[DIAGNOSTICS] LLM call failed, using fallback: {e}", flush=True)
            result = {"root_cause": "Threshold crossing check loops required.", "confidence": 0.5, "contributing_factors": [], "affected_components": [subsys], "risk_assessment": {"immediate_risk": "Medium", "mission_impact": "Partial", "time_to_critical": "Unknown"}, "recommended_actions": [], "similar_known_anomalies": [], "reasoning_chain": []}

        result["anomaly_id"] = anomaly["id"]
        result["subsystem"] = subsys
        result["diagnosed_at"] = datetime.now(timezone.utc).isoformat()
        return result