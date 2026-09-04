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
        violations = []       # NEW violations only -- these are what can block a candidate
        pre_existing = []     # already out of band before this candidate was even proposed

        def _breach(val, meta):
            lo, hi = meta.get("warn_low"), meta.get("warn_high")
            if lo is not None and val < lo:
                return f"{val:.2f} {meta['unit']} below floor {lo} {meta['unit']}"
            if hi is not None and val > hi:
                return f"{val:.2f} {meta['unit']} above ceiling {hi} {meta['unit']}"
            return None

        for param, meta in TELEMETRY_PARAMS.items():
            # Predicted state may not cover every param (simulators commonly
            # only project the ones their candidate touches) -- anything
            # absent is assumed unchanged from current telemetry.
            val = predicted_state.get(param, current_telemetry.get(param))
            if val is None:
                continue

            predicted_breach = _breach(val, meta)
            ok = predicted_breach is None

            # Was this param ALREADY out of band before the candidate was
            # even proposed? If so, the candidate didn't cause it and can't
            # reasonably be blamed for it or expected to fix an unrelated
            # pre-existing condition -- e.g. solar_current_a can drift above
            # its warn_high ceiling at certain orbital sun angles regardless
            # of any fault, and flagging every candidate proposed at that
            # moment as "unsafe" over it would block legitimate, unrelated
            # fixes for no real reason.
            #
            # Second, independent guard: a side effect requires the candidate
            # to have actually, meaningfully MOVED this param. current_telemetry
            # and predicted_state are captured a step apart (simulation time vs
            # baseline-check time) and solar/orbital params keep drifting on
            # their own between those two reads regardless of any candidate --
            # confirmed live, a param sitting within noise-width of its own
            # threshold flipped from "pre-existing" to "newly violating"
            # between two reads of the same still-moving live telemetry, with
            # no candidate delta involved at all. A predicted value within
            # tolerance of the current one was carried over, not caused.
            cur_val = current_telemetry.get(param)
            already_violating = cur_val is not None and _breach(cur_val, meta) is not None
            span = abs(meta.get("max", 100.0) - meta.get("min", 0.0)) or 1.0
            moved_by_candidate = cur_val is None or abs(val - cur_val) > max(1e-6, span * 0.003)
            is_new_violation = (not ok) and not already_violating and moved_by_candidate

            entry = {
                "param": param,
                "subsystem": meta.get("subsystem"),
                "value": val,
                "passed": ok,
                "reason": predicted_breach or "within safe operating band",
                "is_side_effect": is_new_violation and param not in anomaly_params,
                "pre_existing": (not ok) and already_violating,
            }
            checklist.append(entry)
            if is_new_violation:
                violations.append(entry)
            elif entry["pre_existing"]:
                pre_existing.append(entry)

        side_effects = [v for v in violations if v["is_side_effect"]]
        passed = len(violations) == 0

        if passed and pre_existing:
            names = ", ".join(v["param"] for v in pre_existing[:3])
            summary = (
                f"All {len(checklist)} tracked parameters clear for this candidate; "
                f"{len(pre_existing)} parameter(s) ({names}) were already out of band beforehand, "
                f"unrelated to this fix."
            )
        elif passed:
            summary = f"All {len(checklist)} tracked parameters remain within their safe operating band in the predicted post-fix state."
        elif side_effects:
            names = ", ".join(v["param"] for v in side_effects[:3])
            summary = (
                f"Predicted state clears the original anomaly but would NEWLY push {len(side_effects)} "
                f"unrelated parameter(s) out of band ({names}) -- rejected as a side-effect risk."
            )
        else:
            names = ", ".join(v["param"] for v in violations[:3])
            summary = f"Predicted state would newly violate {len(violations)} parameter(s) tied to this incident ({names})."

        return {
            "passed": passed,
            "checked_count": len(checklist),
            "violation_count": len(violations),
            "side_effect_count": len(side_effects),
            "pre_existing_count": len(pre_existing),
            "checklist": checklist,
            "violations": violations,
            "pre_existing": pre_existing,
            "reasoning": summary,
            "trust_score": round(100.0 * (len(checklist) - len(violations)) / len(checklist)) if checklist else 100,
        }
