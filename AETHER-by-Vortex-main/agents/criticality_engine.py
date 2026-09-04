"""
Deterministic Criticality Engine for Satellite Mission Operations.
Calculates a strictly deterministic 0-100 criticality score based on physical telemetry,
subsystem hierarchy, trend velocity, and historical risk.

CRITICAL INVARIANT:
The LLM can NEVER override, downgrade, or bypass this engine's decision.
"""
from typing import Any, Dict, List, Optional
from config import CRITICALITY_POLICIES, CRITICALITY_THRESHOLDS, TELEMETRY_PARAMS

# Subsystem weight multipliers (out of 30 pts)
SUBSYSTEM_WEIGHTS = {
    "COMMS": 30.0,
    "EPS": 28.0,
    "THERMAL": 27.0,
    "OBC": 26.0,
    "ADCS": 22.0,
    "PROPULSION": 18.0,
    "PAYLOAD": 15.0
}


class CriticalityEngine:
    """Calculates deterministic criticality scores and safety policies."""

    def evaluate(
        self,
        subsystem: str,
        violations: List[dict],
        current_telemetry: Dict[str, float],
        history: Dict[str, List[dict]],
        orbital_ctx: Optional[dict] = None,
        rag_similar_incidents: Optional[List[dict]] = None
    ) -> dict:
        subsys_upper = subsystem.upper() if subsystem else "OBC"
        subsys_weight = SUBSYSTEM_WEIGHTS.get(subsys_upper, 20.0)

        # Factor 1: Subsystem base importance (0 - 30 pts)
        score_subsystem = subsys_weight

        # Factor 2: Telemetry deviation severity (0 - 32 pts)
        # Cap raised from 25: at the old cap, even a total comms blackout
        # (SNR -> 0, packet loss -> 100%) landed at the same 25 points as a
        # merely-serious deviation, and the four non-contextual factors summed
        # to a hard ceiling of 85 -- below the 90 needed for CRITICAL. A fault
        # could only ever reach CRITICAL by getting lucky on eclipse timing and
        # RAG history, never on raw severity alone. Verified against the real
        # engine (calibrate.py) that this only moves scores already saturating
        # the old cap -- mild/moderate deviations are unaffected.
        score_deviation = 0.0
        max_dev_ratio = 0.0
        for v in violations:
            param = v.get("param")
            val = v.get("value", 0.0)
            meta = TELEMETRY_PARAMS.get(param, {})
            nominal = meta.get("nominal", val)
            span = abs(meta.get("max", 100.0) - meta.get("min", 0.0)) or 1.0
            dev = abs(val - nominal) / span
            if dev > max_dev_ratio:
                max_dev_ratio = dev
        score_deviation = min(32.0, max_dev_ratio * 40.0)

        # Factor 3: Violation count & multi-point cross-coupling (0 - 18 pts)
        viol_count = len(violations)
        score_multi_point = min(18.0, viol_count * 5.0)

        # Factor 4: Rate of change / acceleration (0 - 15 pts)
        score_rate_of_change = 0.0
        for v in violations:
            param = v.get("param")
            hist = history.get(param, [])
            if len(hist) >= 2:
                v_recent = hist[-1].get("value", 0.0)
                v_prior = hist[-min(4, len(hist))].get("value", 0.0)
                meta = TELEMETRY_PARAMS.get(param, {})
                span = abs(meta.get("max", 100.0) - meta.get("min", 0.0)) or 1.0
                rate = abs(v_recent - v_prior) / span
                score_rate_of_change = max(score_rate_of_change, min(15.0, rate * 35.0))

        # Factor 5: Mission context & eclipse hazard (0 - 10 pts)
        score_context = 0.0
        if orbital_ctx and orbital_ctx.get("in_eclipse"):
            # Anomaly during eclipse is higher risk (no solar generation)
            if subsys_upper in ("EPS", "THERMAL"):
                score_context = 10.0
            else:
                score_context = 5.0

        # Factor 6: Historical recurrence risk from RAG (0 - 5 pts)
        score_history = 0.0
        if rag_similar_incidents:
            high_risk_matches = sum(1 for inc in rag_similar_incidents if inc.get("criticality") in ("HIGH", "CRITICAL"))
            score_history = min(5.0, high_risk_matches * 2.5)

        # Raw composite score
        total_raw = score_subsystem + score_deviation + score_multi_point + score_rate_of_change + score_context + score_history
        # Bound to 0 - 100
        criticality_score = int(max(5, min(100, round(total_raw))))

        # Determine classification
        if criticality_score >= 90:
            severity = "CRITICAL"
        elif criticality_score >= 70:
            severity = "HIGH"
        elif criticality_score >= 40:
            severity = "MEDIUM"
        else:
            severity = "LOW"

        policy = CRITICALITY_POLICIES.get(severity, "HUMAN_APPROVAL_REQUIRED")

        return {
            "criticality_score": criticality_score,
            "severity": severity,
            "policy": policy,
            "requires_human_approval": severity == "CRITICAL",
            "requires_human_oversight": severity == "HIGH",
            "factors": {
                "subsystem_importance": round(score_subsystem, 1),
                "telemetry_deviation": round(score_deviation, 1),
                "multi_point_violations": round(score_multi_point, 1),
                "rate_of_change": round(score_rate_of_change, 1),
                "mission_context": round(score_context, 1),
                "historical_frequency": round(score_history, 1)
            },
            "explanation": f"Score {criticality_score}/100 [{severity}] governed by {subsystem} priority and {viol_count} active violation(s)."
        }
