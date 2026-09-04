"""
Simulator Agent / Digital Twin for Satellite Operations.
Simulates candidate recovery actions forward in time against satellite physics constraints
BEFORE execution.

INVARIANT:
The Simulator Agent never executes commands on the active spacecraft interface.
"""
import json
import random
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from config import COMMAND_WHITELIST, TELEMETRY_PARAMS
from .llm_provider import LLMProvider, safe_number


class SimulatorAgent:
    """Simulates physical telemetry trajectory and validates safety constraints."""

    SIMULATION_FAILURE_RATE = 0.12  # 12% random failure chance

    def __init__(self, llm_provider: LLMProvider):
        self.llm = llm_provider
        self.force_failure: bool = False
        self.force_failure_target_pct: Optional[float] = None

    def trigger_failure(self, target_pct: Optional[float] = None):
        """Manually trigger simulation failure on the next simulation run."""
        self.force_failure = True
        self.force_failure_target_pct = target_pct

    async def simulate_candidates(
        self,
        candidates: List[dict],
        current_telemetry: Dict[str, float],
        orbital_ctx: Optional[dict] = None
    ) -> List[dict]:
        # Check manual or random simulation failure condition
        should_fail = self.force_failure or (random.random() < self.SIMULATION_FAILURE_RATE)
        forced_pct = self.force_failure_target_pct
        self.force_failure = False
        self.force_failure_target_pct = None

        if should_fail:
            simulations = []
            for cand in candidates:
                action_id = cand.get("action_id", "ACT-01")
                simulations.append({
                    "action_id": action_id,
                    "candidate_name": cand.get("name", "Action"),
                    "safe": False,
                    "sim_failed": True,
                    "failure_type": "PHYSICS_DIVERGENCE",
                    "predicted_state": current_telemetry,
                    "constraint_results": ["FAIL: Digital twin physics simulation diverged / numerical instability"],
                    "recovery_probability": 0.08,
                    "risk_score": 96,
                    "reason": "Digital twin forward state solver divergence (numerical matrix singularity).",
                    "trust_score": 5,
                    "forced_solution_pct": forced_pct,
                    "llm_mode": "SIM_FAILED",
                    "simulated_at": datetime.now(timezone.utc).isoformat()
                })
            return simulations

        simulations = []
        for cand in candidates:
            sim = await self._simulate_one(cand, current_telemetry, orbital_ctx)
            simulations.append(sim)
        return simulations

    async def _simulate_one(
        self,
        candidate: dict,
        current_telemetry: Dict[str, float],
        orbital_ctx: Optional[dict] = None
    ) -> dict:
        action_id = candidate.get("action_id", "ACT-01")

        # 1. Deterministic forward simulation
        rule_sim = self.llm.rule_engine.simulate(candidate, current_telemetry)

        # 2. LLM Simulation Review (validating subtle edge cases, cross-coupling)
        def _rule_fallback():
            return rule_sim

        prompt = f"""You are a satellite digital-twin simulation engineer.
Assess whether the proposed recovery procedure produces a safe forward telemetry state.

CANDIDATE PROCEDURE:
{json.dumps(candidate, indent=2)}

CURRENT TELEMETRY:
{json.dumps(current_telemetry, indent=2)}

PHYSICS FORWARD PROJECTION:
{json.dumps(rule_sim, indent=2)}

Respond ONLY with a valid JSON object (no markdown) with these keys:
- safe: boolean (must be false if hard constraints are violated)
- risk_score: integer 0-100 (0=no risk, 100=extreme risk)
- recovery_probability: float 0.0-1.0
- constraint_results: list of strings (e.g. "PASS: Battery SOC > 30%", "PASS: Thermal limits OK")
- reason: concise rationale for simulation clearance or rejection
"""
        system = "You are a spacecraft digital-twin safety evaluator. Output only raw valid JSON."

        llm_sim, mode_used = await self.llm.generate_json(
            prompt=prompt,
            system_instruction=system,
            agent_role="SIMULATOR",
            fallback_handler=_rule_fallback,
            timeout=14.0
        )

        # Invariant: If hard physical constraints failed in deterministic check, safe CANNOT be True
        hard_safe = rule_sim.get("safe", False)
        is_safe = bool(llm_sim.get("safe", hard_safe)) and hard_safe

        # Same invariant extended to the two numbers that feed the safety gate
        # (validator.py rejects risk_score > 75): the LLM tier is untrusted
        # input, so it may only ever push these toward MORE caution than the
        # deterministic physics baseline, never less — it can raise the
        # apparent risk or lower the apparent recovery odds, but a hallucinated
        # "actually this is safer than the physics model thinks" is discarded.
        rule_risk = safe_number(rule_sim.get("risk_score"), default=20, lo=0, hi=100)
        llm_risk = safe_number(llm_sim.get("risk_score"), default=rule_risk, lo=0, hi=100)
        risk_score = int(max(rule_risk, llm_risk))

        rule_prob = safe_number(rule_sim.get("recovery_probability"), default=0.90, lo=0.0, hi=1.0)
        llm_prob = safe_number(llm_sim.get("recovery_probability"), default=rule_prob, lo=0.0, hi=1.0)
        recovery_probability = min(rule_prob, llm_prob)

        return {
            "action_id": action_id,
            "candidate_name": candidate.get("name", "Action"),
            "safe": is_safe,
            "predicted_state": rule_sim.get("predicted_state", current_telemetry),
            "constraint_results": llm_sim.get("constraint_results", rule_sim.get("constraint_results", [])),
            "recovery_probability": recovery_probability,
            "risk_score": risk_score,
            "reason": str(llm_sim.get("reason", rule_sim.get("reason", "Forward simulation verified."))),
            "trust_score": round((recovery_probability * 100 + (100 - risk_score)) / 2) if is_safe else round(recovery_probability * 100 * 0.4),
            "llm_mode": mode_used,
            "simulated_at": datetime.now(timezone.utc).isoformat()
        }
