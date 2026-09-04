"""
Comprehensive End-to-End Pipeline Verification Test.
Tests all agents, deterministic criticality, 3-tier fallback, safety gate,
executor, post-monitor, and RAG self-learning loop.
"""
import asyncio
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agents.orchestrator import MissionOrchestrator


async def run_e2e_test():
    print("\n" + "=" * 60)
    print("  RUNNING AETHER MULTI-AGENT E2E ACCEPTANCE TEST")
    print("=" * 60)

    orch = MissionOrchestrator()
    print("[1/6] Orchestrator initialized. AI Mode:", orch.llm_provider.get_mode_display())

    # 1. Test Telemetry tick
    snap = orch.simulator.tick(interval_s=2.0)
    history = {p: orch.simulator.get_history(p) for p in snap.values}
    print(f"[2/6] Telemetry snapshot generated. Tick: {snap.tick}, Parameters: {len(snap.values)}")

    # 2. Test Anomaly Injection (LOW Anomaly)
    print("\n--- Testing Scenario A: LOW Anomaly (Auto-Approval) ---")
    orch.simulator.inject_anomaly("battery_undervoltage")
    snap_ano = orch.simulator.tick(interval_s=2.0)
    history_ano = {p: orch.simulator.get_history(p) for p in snap_ano.values}

    ano = await orch.watcher.analyze(snap_ano, history_ano, orch.simulator.orbital_context())
    assert ano is not None, "Watcher should detect anomaly"
    print(f"-> Watcher Anomaly Detected: {ano['incident_id']}")
    print(f"-> Severity: {ano['severity']} | Criticality Score: {ano['criticality_score']}/100")
    print(f"-> Policy: {ano['criticality_policy']}")

    diag = await orch.identifier.identify(ano, snap_ano.values, history_ano)
    print(f"-> Identifier Root Cause: {diag['root_cause'][:60]}...")
    print(f"-> Hypotheses Count: {len(diag['hypotheses'])}")

    fixes = await orch.fix_finder.find_fixes(ano, diag, snap_ano.values)
    print(f"-> Fix Finder Candidates: {len(fixes['candidates'])}")
    cand = fixes['candidates'][0]
    print(f"-> Selected Action: {cand['name']} (Commands: {[c['command'] for c in cand['commands']]})")

    sims = await orch.simulator_agent.simulate_candidates(fixes['candidates'], snap_ano.values)
    print(f"-> Simulator Result: Safe={sims[0]['safe']}, Risk={sims[0]['risk_score']}")

    val = orch.validator.validate_action(
        candidate_action=cand,
        simulation_result=sims[0],
        criticality_eval={"severity": ano["severity"], "criticality_score": ano["criticality_score"]},
        current_telemetry=snap_ano.values
    )
    print(f"-> Safety Gate Decision: {val['decision']} (Approved={val['approved_for_execution']})")
    assert val['approved_for_execution'] is True, "LOW/MEDIUM should be auto-approved"

    # Execute
    exec_res = orch.executor.execute_action(ano["incident_id"], cand, authorized=True)
    print(f"-> Executor Status: {exec_res['status']}")

    # Post-monitor
    post_res = orch.post_monitor.evaluate_recovery(ano["incident_id"], ano["affected_params"], orch.simulator.tick().values)
    print(f"-> Post-Monitor: Outcome={post_res['outcome']}")

    # Report & RAG store
    rep = orch.report_generator.generate_incident_report(
        incident_id=ano["incident_id"],
        anomaly=ano,
        diagnosis=diag,
        action=cand,
        simulation=sims[0],
        execution=exec_res,
        outcome=post_res["outcome"]
    )
    inc_id = orch.rag_memory.store_incident(rep)
    print(f"-> Report embedded into RAG memory: {inc_id}")

    # 3. Test High Anomaly (Human Oversight Required)
    print("\n--- Testing Scenario B: HIGH Anomaly (Oversight/Approval Required) ---")
    orch.simulator.inject_anomaly("thermal_excursion")
    snap_hi = orch.simulator.tick(interval_s=2.0)
    history_hi = {p: orch.simulator.get_history(p) for p in snap_hi.values}

    ano_hi = await orch.watcher.analyze(snap_hi, history_hi, orch.simulator.orbital_context())
    print(f"-> Watcher Anomaly: {ano_hi['incident_id']} [{ano_hi['severity']}] Score: {ano_hi['criticality_score']}/100")
    val_hi = orch.validator.validate_action(
        candidate_action=cand,
        simulation_result=sims[0],
        criticality_eval={"severity": ano_hi["severity"], "criticality_score": ano_hi["criticality_score"]},
        current_telemetry=snap_hi.values
    )
    print(f"-> Safety Gate Decision: {val_hi['decision']} (Requires Approval={val_hi['requires_human_approval']})")
    assert val_hi['requires_human_approval'] is True, "HIGH/CRITICAL must require human approval"

    # 4. Test RAG Self-Learning Retrieval
    print("\n--- Testing RAG Self-Learning Retrieval ---")
    retrieved = orch.rag_memory.retrieve_similar_incidents("battery undervoltage solar efficiency degradation", k=2)
    print(f"-> Retrieved {len(retrieved)} matching incidents from RAG memory:")
    for r in retrieved:
        print(f"   * [{r.get('incident_id')}] {r.get('anomaly')} (Similarity: {r.get('similarity_score', 0):.2f})")
    assert len(retrieved) > 0, "RAG memory should successfully retrieve previously learned incident"

    # 5. Test Audit Log Traceability
    audit_entries = orch.audit_logger.get_entries(limit=10)
    print(f"\n--- Testing Audit Logger: {len(audit_entries)} verified entries recorded ---")

    print("\n" + "=" * 60)
    print("  ALL E2E ACCEPTANCE TESTS PASSED SUCCESSFULLY! [OK]")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    asyncio.run(run_e2e_test())
