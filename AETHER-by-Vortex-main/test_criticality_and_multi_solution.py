"""
Verification script for Criticality Ranking Continuity and Multi-Solution Approval Choice.
"""
import asyncio
import sys

import os
os.environ["FORCE_RULE_ENGINE"] = "1"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agents.orchestrator import MissionOrchestrator
from config import ANOMALY_SCENARIOS


async def test_criticality_ranking_continuity():
    print("\n--- 1. Testing Criticality Ranking Continuity Across Scenarios ---")
    orch = MissionOrchestrator()

    test_scenarios = [
        ("comms_degradation", "CRITICAL"),
        ("comms_loss", "CRITICAL"),
        ("full_offline_test", "CRITICAL"),
        ("battery_overtemperature", "HIGH"),
        ("gps_loss", "HIGH"),
        ("solar_array_degradation", "MEDIUM"),
        ("battery_undervoltage", "LOW"),
    ]

    for key, expected_sev in test_scenarios:
        orch.simulator.reset = lambda: None  # mock
        orch.simulator = orch.simulator.__class__()
        orch.simulator.inject_anomaly(key)
        for _ in range(5):
            snap = orch.simulator.tick()
        history = {p: orch.simulator.get_history(p) for p in snap.values}
        orbital_ctx = orch.simulator.orbital_context()

        result = await orch.watcher.analyze(snap, history, orbital_ctx, anomaly_type_hint=key)
        assert result is not None, f"Watcher failed to detect {key}"

        score = result["criticality_score"]
        sev = result["severity"]
        policy = result["criticality_policy"]

        print(f"  [CHECK] {key:25} Labeled: {expected_sev:8} -> Score: {score:3}/100, Sev: {sev:8}, Policy: {policy}")

        assert sev == expected_sev, f"Mismatch for {key}: expected {expected_sev}, got {sev} (Score {score})"
        if expected_sev == "CRITICAL":
            assert score >= 90, f"Critical anomaly {key} must have score >= 90, got {score}"
            assert policy == "HUMAN_APPROVAL_REQUIRED"
        elif expected_sev == "HIGH":
            assert 70 <= score <= 89, f"High anomaly {key} must have 70 <= score <= 89, got {score}"
            assert policy == "HUMAN_OVERSIGHT"
        elif expected_sev == "LOW":
            assert score <= 39, f"Low anomaly {key} must have score <= 39, got {score}"
            assert policy == "AUTO_APPROVED"

    print("  => All scenario severity continuity assertions PASSED!")


async def test_multi_solution_selection():
    print("\n--- 2. Testing Multi-Solution Operator Choice & Action ID Execution ---")
    orch = MissionOrchestrator()
    orch.simulator.inject_anomaly("battery_undervoltage")
    snap = orch.simulator.tick()
    history = {p: orch.simulator.get_history(p) for p in snap.values}
    orbital_ctx = orch.simulator.orbital_context()

    # Detect anomaly
    ano = await orch.watcher.analyze(snap, history, orbital_ctx, anomaly_type_hint="battery_undervoltage")
    diag = await orch.identifier.identify(ano, snap.values, history, orbital_ctx)
    fix_result = await orch.fix_finder.find_fixes(ano, diag, snap.values, orbital_ctx)

    candidates = fix_result.get("candidates", [])
    print(f"  Fix Finder synthesized {len(candidates)} candidates:")
    for i, c in enumerate(candidates):
        print(f"    Option {i+1}: [{c.get('action_id')}] {c.get('name')} (Cmds: {[cmd['command'] for cmd in c.get('commands', [])]})")

    assert len(candidates) >= 2, "Expected at least 2 candidate solutions"

    simulations = await orch.simulator_agent.simulate_candidates(candidates, snap.values, orbital_ctx)
    # Ensure Option 2 simulation is marked safe for the approval test
    simulations[1]["safe"] = True
    simulations[1]["risk_score"] = 20
    simulations[1]["reason"] = "Trajectory nominal"

    # Set up pending approval with multiple candidates
    inc_id = ano["incident_id"]
    chosen_candidate, chosen_sim = candidates[0], simulations[0]
    validation = orch.validator.validate_action(
        candidate_action=chosen_candidate,
        simulation_result=chosen_sim,
        criticality_eval={"severity": "CRITICAL", "criticality_score": 95},
        current_telemetry=snap.values,
        is_human_authorized=False
    )

    orch.pending_approvals[inc_id] = {
        "incident_id": inc_id,
        "anomaly": ano,
        "diagnosis": diag,
        "candidate": chosen_candidate,
        "simulation": chosen_sim,
        "validation": validation,
        "candidates": candidates,
        "simulations": simulations,
        "attempt": 1,
        "requested_at": "now"
    }

    # Test approving Candidate 2 explicitly
    target_cand = candidates[1]
    target_action_id = target_cand["action_id"]
    print(f"  Operator selecting Option 2: {target_action_id} ('{target_cand.get('name')}')")

    # Hook verify job to verify the chosen candidate passed to execution
    dispatched_candidate = None

    async def mock_run_verify_job(subsys, ano_id, anom, diagnosis, chosen_c, chosen_s, val, attempt):
        nonlocal dispatched_candidate
        dispatched_candidate = chosen_c

    orch._run_verify_job = mock_run_verify_job

    success = await orch.approve_procedure(inc_id, action_id=target_action_id)
    assert success is True, "approve_procedure failed"
    assert inc_id not in orch.pending_approvals, "Incident should be removed from pending_approvals"

    # Worker queue has the verify job, pull it
    _, job = await orch.pipeline_queue.get()
    await job()

    assert dispatched_candidate is not None, "Verify job was not invoked"
    assert dispatched_candidate["action_id"] == target_action_id, (
        f"Dispatched candidate {dispatched_candidate['action_id']} != selected {target_action_id}"
    )
    print(f"  => Correctly routed and dispatched chosen Candidate 2: {dispatched_candidate['name']}")
    print("  => Multi-solution operator choice test PASSED!")


async def main():
    await test_criticality_ranking_continuity()
    await test_multi_solution_selection()
    print("\n" + "=" * 60)
    print("  ALL VERIFICATION CHECKS PASSED SUCCESSFULLY!")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
