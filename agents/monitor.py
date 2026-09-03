"""
Telemetry Monitor Agent
"""
import json
import re
from datetime import datetime, timezone
from typing import Optional

from config import MONITOR_MODEL, TELEMETRY_PARAMS
from telemetry.simulator import TelemetrySnapshot
from .llm import acreate, get_client

class MonitorAgent:
    def __init__(self):
        self.self_client = get_client()
        self.self_consecutive_normal = 0

    async def analyze(self, snapshot: TelemetrySnapshot, history: dict) -> Optional[dict]:
        violations = snapshot.violations()
        if not violations:
            self.self_consecutive_normal += 1
            return None

        self.self_consecutive_normal = 0

        current_str = json.dumps(
            {p: {"v": snapshot.values[p], "u": TELEMETRY_PARAMS[p]["unit"]} for p in snapshot.values},
            indent=None,
        )
        viols_str = json.dumps(violations, indent=None)
        hist_summary = {
            p: [round(r["value"], 2) for r in history.get(p, [])[-6:]]
            for p in [v["param"] for v in violations]
        }
        hist_str = json.dumps(hist_summary, indent=None)

        prompt = f"""You are a satellite telemetry monitor agent. Analyze the following data and classify the anomaly.

CURRENT TELEMETRY: {current_str}
THRESHOLD VIOLATIONS: {viols_str}
RECENT TREND (last 6 samples per affected parameter): {hist_str}
ORBITAL CONTEXT: eclipse={snapshot.in_eclipse}, phase={round(snapshot.orbital_phase * 100, 1)}%

Respond ONLY with a JSON object (no markdown) with these keys:
- severity: one of NOMINAL, LOW, MEDIUM, HIGH, CRITICAL
- primary_subsystem: most affected subsystem code (EPS/ADCS/COMMS/THERMAL/OBC/PROPULSION/PAYLOAD)
- anomaly_type: short label (e.g. "battery_undervoltage", "thermal_excursion")
- affected_params: list of parameter names involved
- trend: "stable" | "worsening" | "improving"
- confidence: 0.0-1.0
- summary: one sentence describing the anomaly
"""

        try:
            resp = await acreate(
                self.self_client,
                model=MONITOR_MODEL,
                # 512 truncated the JSON mid-`summary`, which dropped the agent
                # into the rule-based fallback. That fallback can only emit
                # LOW/MEDIUM, so HIGH and CRITICAL anomalies were being
                # silently downgraded and never routed for human approval.
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
                # Detection sits on the critical path: the orchestrator skips
                # new checks while one is in flight, so a long hang here means
                # a short-lived anomaly can expire undetected. Fail fast and
                # let the next tick retry instead of stalling the loop.
                timeout=25.0,
            )
            raw = resp.content[0].text.strip()
            raw = re.sub(r"^```json\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
            result = json.loads(raw)
        except Exception as e:
            print(f"[MONITOR] LLM call failed, using rule-based fallback: {e}", flush=True)
            result = {
                "severity": "MEDIUM" if len(violations) >= 2 else "LOW",
                "primary_subsystem": violations[0]["subsystem"],
                "anomaly_type": "threshold_violation",
                "affected_params": [v["param"] for v in violations],
                "trend": "stable",
                "confidence": 0.6,
                "summary": f"Threshold violation detected on {violations[0]['param']}",
            }

        # Some models return lowercase severities; the approval router matches
        # against uppercase SEVERITY_ORDER and would silently treat an unknown
        # value as LOW, auto-approving an anomaly that needs a human.
        if isinstance(result.get("severity"), str):
            result["severity"] = result["severity"].strip().upper()
        if isinstance(result.get("primary_subsystem"), str):
            result["primary_subsystem"] = result["primary_subsystem"].strip().upper()

        result["id"] = f"ANO-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:12]}"
        result["detected_at"] = datetime.now(timezone.utc).isoformat()
        result["violations"] = violations
        result["tick"] = snapshot.tick
        return result