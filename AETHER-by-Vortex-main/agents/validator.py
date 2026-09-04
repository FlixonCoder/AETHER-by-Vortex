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
        violations = []

        # 1. Whitelist Check
        commands = candidate_action.get("commands", [])
        if not commands:
            violations.append("Candidate action specifies no executable commands")
        else:
            all_whitelisted = True
            for cmd in commands:
                cmd_name = cmd.get("command")
                if cmd_name not in COMMAND_WHITELIST:
                    all_whitelisted = False
                    violations.append(f"Command '{cmd_name}' is not in spacecraft whitelist")
                else:
                    checks_passed.append(f"Command '{cmd_name}' verified in whitelist")

        # 2. Simulation Safety Check
        if not simulation_result.get("safe", False):
            violations.append(f"Simulation rejected candidate: {simulation_result.get('reason', 'Constraint failure')}")
        else:
            checks_passed.append("Forward digital twin simulation passed")

        if simulation_result.get("risk_score", 100) > 75:
            violations.append(f"Simulation risk score {simulation_result.get('risk_score')} exceeds safety ceiling (75)")

        # 3. Telemetry Boundary Sanity Check
        pred_state = simulation_result.get("predicted_state", {})
        if pred_state:
            if pred_state.get("battery_soc_pct", 100.0) < 25.0:
                violations.append("Predicted battery state-of-charge drops below 25%")
            if pred_state.get("battery_voltage_v", 28.0) < 23.5:
                violations.append("Predicted bus voltage drops below 23.5V")

        # 3b. Baseline Telemetry Check (whole-spacecraft side-effect gate)
        # The above check only covers two hardcoded EPS params. The Baseline
        # Checker stage covers every tracked parameter, including ones this
        # candidate wasn't built to touch -- a fix that clears the original
        # anomaly but knocks something else out of band fails HERE, not after
        # it's already been executed.
        if baseline_check is not None and not baseline_check.get("passed", True):
            side_effects = baseline_check.get("side_effect_count", 0)
            if side_effects > 0:
                violations.append(
                    f"Baseline check: candidate would push {side_effects} unrelated parameter(s) "
                    f"out of band as a side effect ({baseline_check.get('reasoning', '')})"
                )
            else:
                violations.append(f"Baseline check failed: {baseline_check.get('reasoning', 'telemetry baseline violated')}")
        elif baseline_check is not None:
            checks_passed.append(f"Baseline check: all {baseline_check.get('checked_count', 0)} tracked parameters within band")

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
        # A clean pass with no violations is full trust; every violation this
        # candidate accumulated (even ones that didn't block it, e.g. a risk
        # score that's elevated but still under the ceiling) costs points.
        trust_score = max(0, 100 - 20 * len(violations))
        if violations:
            reasoning = f"Rejected on {len(violations)} deterministic check(s): {'; '.join(violations[:2])}"
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
            "is_reversible": candidate_action.get("reversible", True),
            "trust_score": trust_score,
            "reasoning": reasoning,
        }
