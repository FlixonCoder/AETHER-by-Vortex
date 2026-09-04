"""
Recovery Planner Agent
"""
import json
import re
from datetime import datetime, timezone
from config import REASONING_MODEL
from .llm import acreate, get_client

class RecoveryPlannerAgent:
    def __init__(self):
        self.self_client = get_client()

    async def plan(self, anomaly: dict, diagnosis: dict, snapshot_values: dict, orbital_ctx: dict) -> dict:
        prompt = f"""You are a satellite mission operations engineer. Generate recovery procedures.
ANOMALY: {json.dumps(anomaly)}
DIAGNOSIS: {json.dumps(diagnosis)}
STATE: {json.dumps(snapshot_values)}

Generate exactly 3 recovery options ranked by preference (best first).
Respond ONLY with a JSON containing 'options' list with keys (rank, name, approach, steps, estimated_duration_min, success_probability, risk_level, side_effects, constraints_checked, reversible, rationale).
"""
        try:
            resp = await acreate(
                self.self_client,
                model=REASONING_MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            result = json.loads(raw)
        except Exception as e:
            print(f"[RECOVERY_PLANNER] LLM call failed, using fallback: {e}", flush=True)
            result = {"options": [{"rank": 1, "name": "Safe Mode Entry", "approach": "Safe isolation safety entry loops", "steps": ["Enter safe mode state configurations"], "estimated_duration_min": 5, "success_probability": 0.85, "risk_level": "LOW", "side_effects": [], "constraints_checked": [], "reversible": True, "rationale": "Fallback security execution profile point."}], "recommended_option_rank": 1, "planning_notes": "Exception processing configurations used."}

        result["anomaly_id"] = anomaly["id"]
        result["planned_at"] = datetime.now(timezone.utc).isoformat()
        return result