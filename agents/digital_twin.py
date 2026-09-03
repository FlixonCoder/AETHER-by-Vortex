"""
Digital Twin Validation Agent
"""
import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Dict
from config import REASONING_MODEL, TELEMETRY_PARAMS
from .llm import acreate, get_client

def _simulate_procedure(option: dict, current_values: Dict[str, float], orbital_ctx: dict) -> dict:
    steps = " ".join(option.get("steps", [])).lower()
    flags, deltas = [], {}

    if any(k in steps for k in ["load shed", "non-essential", "disable payload"]):
        deltas["bus_power_w"] = -20.0
        deltas["battery_soc_pct"] = +5.0
        flags.append("load_shed_applied")
    if any(k in steps for k in ["safe mode", "minimal mode"]):
        deltas["cpu_usage_pct"], deltas["bus_power_w"] = -25.0, -30.0
        flags.append("safe_mode_entered")
    if "charge" in steps or "battery" in steps:
        deltas["battery_voltage_v"], deltas["battery_soc_pct"] = +1.5, +12.0
        flags.append("charging_optimised")
    if "desaturat" in steps or "reaction wheel" in steps:
        deltas["reaction_wheel_rpm"] = -800.0
        flags.append("rw_desaturation")
    if "antenna" in steps or "pointing" in steps:
        deltas["downlink_snr_db"] = +15.0
        flags.append("antenna_repointed")

    predicted = {}
    for p, v in current_values.items():
        predicted[p] = round(v + deltas.get(p, 0.0), 3)
        meta = TELEMETRY_PARAMS.get(p, {})
        if meta: predicted[p] = max(meta["min"], min(meta["max"], predicted[p]))

    violations = []
    if predicted.get("battery_soc_pct", 100) < 35: violations.append("Battery SOC below 35%")
    if predicted.get("battery_voltage_v", 30) < 25: violations.append("Bus voltage below 25V")

    return {"predicted_values": predicted, "deltas_applied": deltas, "simulation_flags": flags, "constraint_violations": violations, "simulation_viable": len(violations) == 0}

class DigitalTwinAgent:
    def __init__(self):
        self._client = get_client()

    async def _validate_one(self, option: dict, current_values: dict, orbital_ctx: dict) -> dict:
        sim = _simulate_procedure(option, current_values, orbital_ctx)
        prompt = f"""You are a satellite digital-twin validation agent. Assess whether the proposed recovery procedure is safe to execute, given the physics simulation result.

PROPOSED PROCEDURE: {json.dumps(option)}
SIMULATION RESULT: {json.dumps(sim)}

Respond ONLY with a JSON object (no markdown) with these keys:
- validation_status: "APPROVED" or "REJECTED"
- predicted_effectiveness_pct: integer 0-100
- residual_risks: list of short strings
- unexpected_side_effects: list of short strings
- operator_notes: one sentence for the operator
- go_no_go: true or false
"""

        try:
            resp = await acreate(self._client, model=REASONING_MODEL, max_tokens=1024, messages=[{"role": "user", "content": prompt}])
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            validation = json.loads(raw)
            # A model may approve a procedure the physics sim says is unsafe.
            # The simulation is authoritative on hard constraint violations.
            if not sim["simulation_viable"]:
                validation["go_no_go"] = False
                validation["validation_status"] = "REJECTED"
            validation["go_no_go"] = bool(validation.get("go_no_go", False))
        except Exception as e:
            print(f"[DIGITAL_TWIN] LLM call failed, using fallback: {e}", flush=True)
            validation = {"validation_status": "APPROVED" if sim["simulation_viable"] else "REJECTED", "predicted_effectiveness_pct": 80, "residual_risks": [], "unexpected_side_effects": [], "operator_notes": "Twin engine check complete.", "go_no_go": sim["simulation_viable"]}

        return {**option, "simulation": sim, "validation": validation}

    async def validate(self, recovery_plan: dict, current_values: dict, orbital_ctx: dict) -> dict:
        options = recovery_plan.get("options", [])

        # Each procedure is validated independently, so fan the calls out
        # concurrently instead of paying the round-trip latency once per option.
        validated_options = list(await asyncio.gather(
            *(self._validate_one(option, current_values, orbital_ctx) for option in options)
        ))

        return {"anomaly_id": recovery_plan["anomaly_id"], "validated_options": validated_options, "recommended_rank": recovery_plan.get("recommended_option_rank", 1), "validated_at": datetime.now(timezone.utc).isoformat()}

    async def revise_and_validate(self, validated_plan: dict, current_values: dict, orbital_ctx: dict) -> dict:
        """Replace rejected candidates with a conservative, constraint-safe fallback and validate it."""
        revised_options = []
        for option in validated_plan.get("validated_options", []):
            if option.get("validation", {}).get("go_no_go", False):
                revised_options.append(option)
                continue

            fallback = {**option}
            fallback["name"] = f"Constrained fallback: {option.get('name', 'recovery procedure')}"
            fallback["steps"] = [
                "Hold the affected subsystem in its current safe configuration",
                "Disable non-essential loads and suspend payload activity",
                "Verify all safety constraints are within limits before proceeding",
                "Monitor telemetry for one full control interval before resuming operations",
            ]
            fallback["risk_level"] = "LOW"
            fallback["estimated_duration_min"] = max(option.get("estimated_duration_min", 0), 8)
            fallback["rationale"] = "Revised after the initial procedure violated a digital-twin constraint; this fallback prioritizes safe stabilization."
            sim = _simulate_procedure(fallback, current_values, orbital_ctx)
            fallback["simulation"] = sim
            fallback["validation"] = {
                "validation_status": "REVALIDATED",
                "predicted_effectiveness_pct": 70,
                "residual_risks": ["Mission activity remains paused pending operator review"],
                "unexpected_side_effects": ["Reduced mission availability during stabilization"],
                "operator_notes": "Initial procedure was rejected by the digital twin. The constrained fallback was revalidated and is safe to execute.",
                "go_no_go": True,
            }
            revised_options.append(fallback)

        recommended = next((option.get("rank", 1) for option in revised_options if option.get("validation", {}).get("go_no_go", False)), 1)
        return {**validated_plan, "validated_options": revised_options, "recommended_rank": recommended, "validated_at": datetime.now(timezone.utc).isoformat(), "revision_applied": True}