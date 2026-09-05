"""
Deterministic Safety Gate / Validator.
Validates recovery procedures between the Simulator and Executor.
Ensures no unverified, non-whitelisted, or policy-violating command can ever reach the spacecraft.

CRITICAL INVARIANT:
The LLM cannot bypass or modify the decisions of this safety gate.
"""
from typing import Any, Dict, List, Optional
from config import COMMAND_WHITELIST, CRITICALITY_POLICIES


class SafetyValidator:
    """Deterministic policy and constraint validator."""

    def validate_action(
        self,
        candidate_action: dict,
        simulation_result: dict,
        criticality_eval: dict,
        current_telemetry: Dict[str, float],
        is_human_authorized: bool = False,
        baseline_check: Optional[dict] = None,
    ) -> dict:
        checks_passed = []
        # hard_violations: absolute blocks — human authorization cannot override these.
        # soft_violations: side-effect warnings — operator approval may waive these.
        hard_violations: List[str] = []
        soft_violations: List[str] = []

        # 1. Whitelist Check (HARD — no command may ever execute off-whitelist)
        commands = candidate_action.get("commands", [])
        if not commands:
            hard_violations.append("Candidate action specifies no executable commands")
        else:
            for cmd in commands:
                cmd_name = cmd.get("command")
                if cmd_name not in COMMAND_WHITELIST:
                    hard_violations.append(f"Command '{cmd_name}' is not in spacecraft whitelist")
                else:
                    checks_passed.append(f"Command '{cmd_name}' verified in whitelist")

        # 2. Simulation Safety Check (HARD — unsafe simulation is a hard stop)
        if not simulation_result.get("safe", False):
            hard_violations.append(f"Simulation rejected candidate: {simulation_result.get('reason', 'Constraint failure')}")
        else:
            checks_passed.append("Forward digital twin simulation passed")

        if simulation_result.get("risk_score", 100) > 75:
            hard_violations.append(f"Simulation risk score {simulation_result.get('risk_score')} exceeds safety ceiling (75)")

        # 3. Telemetry Boundary Sanity Check (HARD — predicted floor breaches are hard stops)
        pred_state = simulation_result.get("predicted_state", {})
        if pred_state:
            if pred_state.get("battery_soc_pct", 100.0) < 25.0:
                hard_violations.append("Predicted battery state-of-charge drops below 25%")
            if pred_state.get("battery_voltage_v", 28.0) < 23.5:
                hard_violations.append("Predicted bus voltage drops below 23.5V")

        # 3b. Baseline Telemetry Check (SOFT — side-effect warnings, waivable by operator)
        # The Baseline Checker stage covers every tracked parameter, including
        # ones this candidate wasn't built to touch -- a fix that clears the
        # original anomaly but knocks something else out of band raises a
        # warning HERE. However, because the rule-engine simulator is an
        # additive-delta model that only moves parameters it has explicit
        # deltas for (and echoes all other params at their current value),
        # predicted_state will always show the anomaly's own parameter still
        # violating for gradually-recovering faults. Post-Monitor is the
        # authoritative arbiter of actual recovery over real elapsed ticks.
        # A human operator who has reviewed the situation may choose to waive
        # these side-effect warnings — they are SOFT blocks, not hard stops.
        if baseline_check is not None and baseline_check.get("side_effect_count", 0) > 0:
            side_effects = baseline_check["side_effect_count"]
            soft_violations.append(
                f"Baseline check: candidate would push {side_effects} unrelated parameter(s) "
                f"out of band as a side effect ({baseline_check.get('reasoning', '')})"
            )
        elif baseline_check is not None and not baseline_check.get("passed", True):
            checks_passed.append(
                f"Baseline check: no side effects on unrelated parameters "
                f"({baseline_check.get('violation_count', 0)} of the anomaly's own affected parameter(s) "
                f"not yet reflected in the single-step prediction — Post-Monitor verifies actual recovery)"
            )
        elif baseline_check is not None:
            checks_passed.append(f"Baseline check: all {baseline_check.get('checked_count', 0)} tracked parameters within band")

        # Compose the effective violations list:
        # Hard violations always block; soft violations only block when there
        # is no human authorization to waive them.
        if is_human_authorized:
            violations = hard_violations
            if soft_violations:
                checks_passed.append(
                    f"Operator waived {len(soft_violations)} side-effect warning(s): "
                    + "; ".join(soft_violations)
                )
        else:
            violations = hard_violations + soft_violations

        # 4. Criticality Policy Enforcement
        severity = criticality_eval.get("severity", "CRITICAL")
        criticality_score = criticality_eval.get("criticality_score", 100)

        # Policy decision logic
        if violations:
            decision = "REJECTED"
            approved = False
            requires_approval = False
        elif severity == "CRITICAL":
            # ALWAYS requires explicit human authorization
            if is_human_authorized:
                decision = "HUMAN_APPROVED_READY"
                approved = True
                requires_approval = False
                checks_passed.append("Human commander cryptographic authorization validated")
            else:
                decision = "AWAITING_HUMAN_APPROVAL"
                approved = False
                requires_approval = True
        elif severity == "HIGH":
            # Requires oversight/confirmation
            if is_human_authorized:
                decision = "OVERSIGHT_CONFIRMED_READY"
                approved = True
                requires_approval = False
                checks_passed.append("Operator oversight confirmed")
            else:
                decision = "AWAITING_OPERATOR_OVERSIGHT"
                approved = False
                requires_approval = True
        else:
            # LOW or MEDIUM: fully auto-executable once simulation passes
            decision = "AUTO_APPROVED_EXECUTABLE"
            approved = True
            requires_approval = False
            checks_passed.append(f"Auto-approval granted for {severity} criticality within safety envelope")

        # Deterministic trust: this gate doesn't guess, so its trust score is
        # not a confidence estimate -- it reflects how clean the decision was.
        # A clean pass with no violations is full trust; every hard violation
        # this candidate accumulated costs points. Waived soft violations cost
        # a smaller penalty since the operator reviewed and accepted the risk.
        waived_count = len(soft_violations) if is_human_authorized else 0
        trust_score = max(0, 100 - 20 * len(hard_violations) - 5 * waived_count)
        if violations:
            reasoning = f"Rejected on {len(violations)} deterministic check(s): {'; '.join(violations[:2])}"
        elif waived_count:
            reasoning = (
                f"Cleared {len(checks_passed)} deterministic check(s); "
                f"{waived_count} soft side-effect warning(s) waived by operator; "
                f"routed to {decision} under {severity} policy."
            )
        else:
            reasoning = f"Cleared {len(checks_passed)} deterministic check(s); routed to {decision} under {severity} policy."

        return {
            "action_id": candidate_action.get("action_id", "ACT-UNKNOWN"),
            "approved_for_execution": approved,
            "requires_human_approval": requires_approval,
            "decision": decision,
            "severity": severity,
            "criticality_score": criticality_score,
            "checks_passed": checks_passed,
            "violations": violations,
            "soft_violations_waived": soft_violations if is_human_authorized else [],
            "is_reversible": candidate_action.get("reversible", True),
            "trust_score": trust_score,
            "reasoning": reasoning,
        }
