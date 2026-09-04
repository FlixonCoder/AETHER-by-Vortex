"""
Post-Execution Monitor Agent.
Monitors spacecraft telemetry post-execution to verify anomaly recovery.
Triggers re-diagnosis cycles if recovery criteria are not met.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from config import TELEMETRY_PARAMS


class PostExecutionMonitor:
    """Monitors telemetry post-command execution to verify state recovery."""

    def evaluate_recovery(
        self,
        incident_id: str,
        affected_params: List[str],
        current_telemetry: Dict[str, float],
        attempt_number: int = 1
    ) -> dict:
        remaining_violations = []

        for param in affected_params:
            if param in current_telemetry and param in TELEMETRY_PARAMS:
                val = current_telemetry[param]
                meta = TELEMETRY_PARAMS[param]
                lo = meta.get("warn_low")
                hi = meta.get("warn_high")
                if lo is not None and val < lo:
                    remaining_violations.append({
                        "param": param, "value": val, "threshold": lo, "direction": "LOW"
                    })
                elif hi is not None and val > hi:
                    remaining_violations.append({
                        "param": param, "value": val, "threshold": hi, "direction": "HIGH"
                    })

        recovered = len(remaining_violations) == 0
        checked = max(1, len(affected_params))
        trust_score = 100 if recovered else max(0, round(100 * (checked - len(remaining_violations)) / checked))

        return {
            "incident_id": incident_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "recovered": recovered,
            "outcome": "RECOVERED" if recovered else "FAILED",
            "attempt_number": attempt_number,
            "remaining_violations": remaining_violations,
            "trust_score": trust_score,
            "status_message": (
                f"Incident {incident_id} telemetry recovered successfully to nominal bands."
                if recovered else
                f"Incident {incident_id} persistent: {len(remaining_violations)} parameter(s) remain out of limits."
            ),
            "reasoning": (
                f"All {checked} originally-affected parameter(s) verified back within their warn band."
                if recovered else
                f"{len(remaining_violations)}/{checked} originally-affected parameter(s) still out of band after attempt #{attempt_number}."
            ),
        }
