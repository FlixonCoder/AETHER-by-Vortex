"""
Baseline Telemetry Checker.
Runs after the Simulator, before the Safety Gate. Checks the simulator's
predicted post-fix state against EVERY tracked telemetry parameter's safe
operating band -- not just the parameters the anomaly itself flagged.

Why this exists: the Simulator only verifies that a candidate resolves the
anomaly it was built for. A fix that clears a battery undervoltage by, say,
shedding a load could push something else (thermal, comms) out of band as a
side effect, and nothing upstream of this stage would have noticed -- the
Simulator would report "safe", the Safety Gate would auto-approve, and the
Post-Monitor would only be watching the ORIGINAL anomaly's own params. This
stage is the one place that looks at the whole spacecraft, not just the part
that was on fire.

Deterministic by design, same invariant as the Safety Gate: this is a plain
threshold check against config.py's TELEMETRY_PARAMS, not an LLM call --
there is nothing for a model to reason about here, only bounds to compare.
"""
from typing import Dict, List, Optional
from config import TELEMETRY_PARAMS


class BaselineChecker:
    """Verifies a candidate's predicted post-fix state against every telemetry baseline."""

    def check(
        self,
        predicted_state: Dict[str, float],
        current_telemetry: Dict[str, float],
        anomaly_params: Optional[List[str]] = None,
    ) -> dict:
        anomaly_params = set(anomaly_params or [])
        checklist = []
        violations = []

        for param, meta in TELEMETRY_PARAMS.items():
            # Predicted state may not cover every param (simulators commonly
            # only project the ones their candidate touches) -- anything
            # absent is assumed unchanged from current telemetry.
            val = predicted_state.get(param, current_telemetry.get(param))
            if val is None:
                continue
            lo, hi = meta.get("warn_low"), meta.get("warn_high")
            ok = True
            reason = "within safe operating band"
            if lo is not None and val < lo:
                ok = False
                reason = f"{val:.2f} {meta['unit']} below floor {lo} {meta['unit']}"
            elif hi is not None and val > hi:
                ok = False
                reason = f"{val:.2f} {meta['unit']} above ceiling {hi} {meta['unit']}"

            entry = {
                "param": param,
                "subsystem": meta.get("subsystem"),
                "value": val,
                "passed": ok,
                "reason": reason,
                "is_side_effect": ok is False and param not in anomaly_params,
            }
            checklist.append(entry)
            if not ok:
                violations.append(entry)

        side_effects = [v for v in violations if v["is_side_effect"]]
        passed = len(violations) == 0

        if passed:
            summary = f"All {len(checklist)} tracked parameters remain within their safe operating band in the predicted post-fix state."
        elif side_effects:
            names = ", ".join(v["param"] for v in side_effects[:3])
            summary = (
                f"Predicted state clears the original anomaly but would push {len(side_effects)} "
                f"unrelated parameter(s) out of band ({names}) -- rejected as a side-effect risk."
            )
        else:
            names = ", ".join(v["param"] for v in violations[:3])
            summary = f"Predicted state still violates {len(violations)} parameter(s) tied to this incident ({names})."

        return {
            "passed": passed,
            "checked_count": len(checklist),
            "violation_count": len(violations),
            "side_effect_count": len(side_effects),
            "checklist": checklist,
            "violations": violations,
            "reasoning": summary,
            "trust_score": round(100.0 * (len(checklist) - len(violations)) / len(checklist)) if checklist else 100,
        }
