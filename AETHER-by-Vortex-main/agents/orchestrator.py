"""
Mission Orchestrator Agent.
Coordinates the closed-loop autonomous satellite operations cycle:
Telemetry -> Watcher -> Criticality -> Identifier -> Fix Finder -> Simulator ->
Validator -> Executor -> Post-Monitor -> (Re-diagnose if failed) -> Report -> RAG Memory -> Audit Log.
"""
import asyncio
import functools
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from config import (
    ANOMALY_CHECK_EVERY_N_TICKS,
    ANOMALY_SCENARIOS,
    MAX_RECOVERY_ATTEMPTS,
    RUNBOOK_DIR,
    TELEMETRY_INTERVAL_S,
)
from telemetry.simulator import SatelliteSimulator
from telemetry.rolling_analyzer import RollingTelemetryAnalyzer

from .audit_logger import AuditLogger
from .criticality_engine import CriticalityEngine
from .executor import CommandExecutor
from .fix_finder import FixFinderAgent
from .identifier import IdentifierAgent
from .llm_provider import LLMProvider
from .post_monitor import PostExecutionMonitor
from .rag_memory import RAGMemory
from .report_generator import ReportGenerator
from .simulator_agent import SimulatorAgent
from .validator import SafetyValidator
from .baseline_checker import BaselineChecker
from .watcher import WatcherAgent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissionOrchestrator:
    """Central orchestrator for autonomous satellite mission operations."""

    def __init__(self):
        self.simulator = SatelliteSimulator()
        self.rag_memory = RAGMemory()
        self.audit_logger = AuditLogger()
        self.criticality_engine = CriticalityEngine()
        self.llm_provider = LLMProvider()

        # Rolling telemetry statistical filter
        self.rolling_analyzer = RollingTelemetryAnalyzer()

        # Agents
        self.watcher = WatcherAgent(self.llm_provider, self.rag_memory, self.criticality_engine)
        self.identifier = IdentifierAgent(self.llm_provider, self.rag_memory)
        self.fix_finder = FixFinderAgent(self.llm_provider, self.rag_memory)
        self.simulator_agent = SimulatorAgent(self.llm_provider)
        self.validator = SafetyValidator()
        self.baseline_checker = BaselineChecker()
        self.executor = CommandExecutor(self.simulator)
        self.post_monitor = PostExecutionMonitor()
        self.report_generator = ReportGenerator()

        # Communication & Broadcasting
        self.broadcast_cb: Optional[Callable] = None

        # State tracking
        self.current_telemetry: Dict[str, float] = {}
        self.active_anomalies: List[Dict] = []
        self.pending_approvals: Dict[str, Dict] = {}
        self.activity_log: List[Dict] = []
        self.runbooks: List[Dict] = []
        if RUNBOOK_DIR.exists():
            for p in sorted(RUNBOOK_DIR.glob("runbook_*.md"), reverse=True):
                parts = p.stem.split("_")
                ano_id = parts[1] if len(parts) > 1 else "ANO-ARCHIVED"
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
                self.runbooks.append({
                    "filename": p.name,
                    "anomaly_id": ano_id,
                    "generated_at": mtime
                })
        self.retry_counts: Dict[str, int] = {}
        # Trust/reasoning trail — one entry per agent stage per incident,
        # collected across every retry attempt. Averaged into a single
        # solution-level trust score and folded into the report + runbook so
        # an operator can see not just what happened but why each stage made
        # the call it did, and how confident that stage was in its own output.
        self.stage_trail: Dict[str, List[dict]] = {}
        self.resolved_incidents: List[Dict] = []
        self.latest_rolling_stats: Dict[str, dict] = {}  # cached for API

        # Single-worker pipeline queue: incidents (initial detection, retries,
        # and human-approved continuations) are processed strictly one at a
        # time. This is deliberate — running independent incidents concurrently
        # made unrelated failures interleave in the activity log and read as
        # one incident stuck in an endless loop. `current_incident_id` and
        # `subsystems_in_flight` exist for callers (dedup, status API) that
        # need to know what's running without reaching into the queue.
        self.pipeline_queue: "asyncio.Queue[tuple[str, Callable[[], Any]]]" = asyncio.Queue()
        self.current_incident_id: Optional[str] = None
        self.subsystems_in_flight: Set[str] = set()

        # Simulation failure condition flag (manual trigger or random fallback)
        self.force_sim_failure: bool = False
        self.force_sim_solution_pct: Optional[float] = None

        self.running = False
        self.tick = 0
        self.monitor_busy = False
        self._last_mode_display: str = ""

    async def run(self):
        self.running = True
        # Initial probe
        await self.llm_provider.probe_availability()
        self._last_mode_display = self.llm_provider.get_mode_display()
        self._log_activity("ORCHESTRATOR", f"AETHER Mission Ops online. AI Tier: {self._last_mode_display}", "info")

        asyncio.create_task(self._pipeline_worker())

        while self.running:
            await self._step()
            await asyncio.sleep(TELEMETRY_INTERVAL_S)

    async def _pipeline_worker(self):
        """Drains the pipeline queue one incident at a time. A retry (the
        Post-Monitor cycling back to the Identifier) runs as a direct `await`
        inside the job coroutine rather than a new queued item, so the worker
        does not consider the incident 'done' — and pull the next one — until
        every retry attempt has actually finished."""
        while True:
            inc_id, job = await self.pipeline_queue.get()
            backlog = self.pipeline_queue.qsize()
            self.current_incident_id = inc_id
            try:
                await job()
            except Exception as e:
                print(f"[ORCHESTRATOR] Pipeline job for {inc_id} failed: {type(e).__name__}: {e}", flush=True)
                self._log_activity("ORCHESTRATOR", f"Pipeline job for {inc_id} raised {type(e).__name__} — see server logs.", "error")
            finally:
                self.current_incident_id = None
                self.pipeline_queue.task_done()

    def stop(self):
        self.running = False

    async def _step(self):
        self.tick += 1
        snap = self.simulator.tick(interval_s=TELEMETRY_INTERVAL_S)
        self.current_telemetry = snap.values

        # Probe AI availability every 2 ticks (4 seconds)
        if self.tick % 2 == 0:
            await self.llm_provider.probe_availability()
            current_disp = self.llm_provider.get_mode_display()
            if current_disp != self._last_mode_display:
                self._last_mode_display = current_disp
                self._log_activity("AI_PROBE", f"Active AI Tier switched: {current_disp}", "info")
                await self._broadcast({
                    "type": "llm_mode_update",
                    "timestamp": _now(),
                    "data": self.llm_provider.get_mode_info()
                })

        # ── Rolling statistical analysis (every tick) ──────────────────────
        violations = snap.violations()
        rolling_result = self.rolling_analyzer.analyze(
            values=snap.values,
            violations=violations,
        )
        self.latest_rolling_stats = self.rolling_analyzer.get_summary()

        # Broadcast telemetry update (with rolling stats attached)
        await self._broadcast({
            "type": "telemetry_update",
            "timestamp": _now(),
            "data": {
                "values": snap.values,
                "units": snap.units,
                "orbital": self.simulator.orbital_context(),
                "violations": violations,
                "llm_mode": self.llm_provider.current_mode,
                "llm_display": self.llm_provider.get_mode_display(),
                "llm_info": self.llm_provider.get_mode_info(),
                "rolling_stats": self.latest_rolling_stats,
                "rolling_analysis": {
                    "persistent_anomalies": rolling_result.persistent_anomalies,
                    "transient_spikes": rolling_result.transient_spikes,
                    "emergency_params": rolling_result.emergency_params,
                    "suppressed": rolling_result.suppressed_violations,
                },
                "pipeline_status": {
                    "current_incident_id": self.current_incident_id,
                    "queued_incident_count": self.pipeline_queue.qsize(),
                }
            }
        })

        # Log transient spikes (suppressed from pipeline)
        if rolling_result.transient_spikes:
            self._log_activity(
                "ROLLING_FILTER",
                f"Transient spike(s) suppressed: {', '.join(rolling_result.transient_spikes)}",
                "info"
            )

        # Check telemetry violations periodically — only if genuine/emergency
        has_genuine = bool(
            rolling_result.persistent_anomalies or rolling_result.emergency_params
        )
        if self.tick % ANOMALY_CHECK_EVERY_N_TICKS == 0 and not self.monitor_busy and has_genuine:
            history = {p: self.simulator.get_history(p) for p in snap.values}
            asyncio.create_task(self._check_and_process_telemetry(snap, history))

    async def _check_and_process_telemetry(self, snap, history: dict):
        self.monitor_busy = True
        try:
            orbital_ctx = self.simulator.orbital_context()
            active_ano = self.simulator.get_active_anomaly()
            hint = active_ano.get("key") if active_ano else None
            anomaly = await self.watcher.analyze(snap, history, orbital_ctx, anomaly_type_hint=hint)

            if anomaly and anomaly.get("anomaly_detected"):
                if not self._is_duplicate_anomaly(anomaly):
                    self.active_anomalies.append(anomaly)
                    self._record_stage(anomaly["incident_id"], "WATCHER", anomaly, 1)
                    self._log_activity(
                        "WATCHER",
                        f"Anomaly detected [{anomaly['severity']}]: {anomaly['summary']} (Score: {anomaly['criticality_score']}/100)",
                        anomaly["severity"].lower()
                    )

                    self.audit_logger.log(
                        incident_id=anomaly["incident_id"],
                        agent="WATCHER",
                        action="ANOMALY_DETECTED",
                        input_data={"violations": anomaly.get("violations")},
                        output_data=anomaly,
                        rag_context_ids=[m.get("incident_id") for m in anomaly.get("rag_matches", [])],
                        criticality={"score": anomaly["criticality_score"], "severity": anomaly["severity"]},
                        llm_mode=anomaly.get("llm_mode")
                    )

                    await self._broadcast({"type": "anomaly_detected", "timestamp": _now(), "data": anomaly})

                    backlog = self.pipeline_queue.qsize() + (1 if self.current_incident_id else 0)
                    if backlog > 0:
                        self._log_activity(
                            "ORCHESTRATOR",
                            f"Incident {anomaly['incident_id']} queued behind {backlog} in-progress pipeline(s) — processed one at a time.",
                            "info"
                        )
                    self.subsystems_in_flight.add(anomaly["primary_subsystem"])
                    await self.pipeline_queue.put((
                        anomaly["incident_id"],
                        functools.partial(self._run_pipeline_job, anomaly, snap)
                    ))
        except Exception as e:
            print(f"[WATCHER] Error during telemetry check: {type(e).__name__}: {e}", flush=True)
        finally:
            self.monitor_busy = False

    async def _run_pipeline_job(self, anomaly: dict, snap):
        """Wraps _execute_pipeline so the in-flight subsystem marker is
        cleared once the incident actually reaches a terminal state.

        _execute_pipeline returns as soon as it routes to human approval,
        NOT once the incident is resolved -- the underlying fault is still
        live and still out of band while a human hasn't acted yet. Clearing
        the marker at that point let the watcher re-detect the same
        still-unresolved fault a few ticks later as what looked like a
        second incident (compounded by incident_id's minute-level
        granularity, which could even collide with the first one's id). Stay
        in-flight for as long as the incident sits in pending_approvals; an
        unhandled exception still clears it via the `finally`, so a crashed
        pipeline can't permanently block the subsystem either.
        """
        inc_id = anomaly["incident_id"]
        subsys = anomaly["primary_subsystem"]
        try:
            await self._execute_pipeline(anomaly, snap)
        finally:
            if inc_id not in self.pending_approvals:
                self.subsystems_in_flight.discard(subsys)

    def _is_duplicate_anomaly(self, new_anomaly: dict) -> bool:
        subsys = new_anomaly.get("primary_subsystem")
        # A pipeline is already running or queued for this subsystem — the
        # existing incident will surface any still-unresolved fault when it
        # completes (or re-queues via the retry path); detecting a second one
        # for the same subsystem right now would just pile up in the queue.
        if subsys in self.subsystems_in_flight:
            return True
        for existing in self.active_anomalies[-5:]:
            if (existing.get("primary_subsystem") == subsys and
                    abs(existing.get("tick", 0) - new_anomaly.get("tick", 0)) < 20):
                return True
        return False

    async def _execute_pipeline(self, anomaly: dict, snap, attempt: int = 1):
        """Full closed-loop agent pipeline."""
        inc_id = anomaly["incident_id"]
        self.retry_counts[inc_id] = attempt
        orbital_ctx = self.simulator.orbital_context()
        # Single source of truth for every audit entry this incident produces —
        # the criticality engine's verdict never changes mid-incident, so every
        # agent step in the audit trail should carry the same score, not just
        # the two steps that happened to compute one locally.
        criticality_ctx = {"score": anomaly.get("criticality_score"), "severity": anomaly.get("severity")}

        # -------------------------------------------------------------
        # Step 1: Identifier Agent (Root Cause & Hypotheses)
        # -------------------------------------------------------------
        self._log_activity("IDENTIFIER", f"Analyzing root cause for incident {inc_id} (Attempt #{attempt})...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "IDENTIFIER", "incident_id": inc_id, "status": "running", "message": "Formulating diagnostic hypotheses..."}})

        history = {p: self.simulator.get_history(p) for p in snap.values}
        diagnosis = await self.identifier.identify(anomaly, snap.values, history, orbital_ctx)
        diagnosis["incident_id"] = inc_id
        self._record_stage(inc_id, "IDENTIFIER", diagnosis, attempt)

        self._log_activity("IDENTIFIER", f"Root cause: {diagnosis.get('root_cause')[:90]}...", "success")
        await self._broadcast({"type": "diagnosis_complete", "timestamp": _now(), "data": diagnosis})

        self.audit_logger.log(
            incident_id=inc_id,
            agent="IDENTIFIER",
            action="ROOT_CAUSE_DIAGNOSED",
            input_data={"anomaly": anomaly},
            output_data=diagnosis,
            rag_context_ids=diagnosis.get("rag_context_ids", []),
            criticality=criticality_ctx,
            llm_mode=diagnosis.get("llm_mode")
        )

        # -------------------------------------------------------------
        # Step 2: Fix Finder Agent (Candidate Actions)
        # -------------------------------------------------------------
        self._log_activity("FIX_FINDER", f"Retrieving recovery options and matching whitelist commands...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "FIX_FINDER", "incident_id": inc_id, "status": "running", "message": "Generating candidate recovery procedures..."}})

        fix_result = await self.fix_finder.find_fixes(anomaly, diagnosis, snap.values, orbital_ctx)
        candidates = fix_result.get("candidates", [])
        self._record_stage(inc_id, "FIX_FINDER", fix_result, attempt)
        self._log_activity("FIX_FINDER", f"Synthesized {len(candidates)} candidate action(s)", "success")
        await self._broadcast({"type": "fix_options_ready", "timestamp": _now(), "data": fix_result})

        self.audit_logger.log(
            incident_id=inc_id,
            agent="FIX_FINDER",
            action="CANDIDATE_ACTIONS_SYNTHESIZED",
            input_data={"diagnosis": diagnosis},
            output_data=fix_result,
            criticality=criticality_ctx,
            llm_mode=fix_result.get("llm_mode")
        )

        # -------------------------------------------------------------
        # Step 3: Simulator Agent (Digital Twin Forward Evaluation)
        # -------------------------------------------------------------
        self._log_activity("SIMULATOR", "Running digital twin physics simulations on candidates...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "SIMULATOR", "incident_id": inc_id, "status": "running", "message": "Evaluating forward state trajectories..."}})

        simulations = await self.simulator_agent.simulate_candidates(candidates, snap.values, orbital_ctx)
        for s in simulations:
            self._record_stage(inc_id, "SIMULATOR", s, attempt)
        await self._broadcast({"type": "simulation_complete", "timestamp": _now(), "data": {"incident_id": inc_id, "simulations": simulations}})

        # -------------------------------------------------------------
        # Step 3a: Simulation Failure Fallback to Rule-Based System
        # Triggered randomly or manually when simulation fails / diverged.
        # If rule-based solution % > 60: proceed without approval; else ask user.
        # -------------------------------------------------------------
        sim_failed = any(s.get("sim_failed", False) for s in simulations) or self.force_sim_failure
        forced_pct = self.force_sim_solution_pct
        if self.force_sim_failure:
            self.force_sim_failure = False
            self.force_sim_solution_pct = None
            sim_failed = True

        if sim_failed:
            self._log_activity(
                "SIMULATOR",
                "Digital twin physics simulation failed / diverged! Falling back to deterministic Rule-Based System...",
                "warning"
            )
            await self._broadcast({
                "type": "sim_failure_fallback",
                "timestamp": _now(),
                "data": {
                    "incident_id": inc_id,
                    "message": "Simulation diverged; engaged rule-based recovery engine."
                }
            })

            rule_evals = []
            for cand in candidates:
                r_sim = self.llm_provider.rule_engine.simulate(cand, snap.values)
                base_prob = cand.get("estimated_recovery_probability", r_sim.get("recovery_probability", 0.90))
                if not r_sim.get("safe", False):
                    sol_pct = 28.0
                elif forced_pct is not None:
                    sol_pct = float(forced_pct)
                else:
                    crit_score = anomaly.get("criticality_score", 50)
                    if crit_score >= 85:
                        # High criticality degrades rule-based confidence into 42-56% (<=60%)
                        sol_pct = round(max(35.0, min(56.0, base_prob * 100.0 - (crit_score - 40) * 0.7)), 1)
                    else:
                        # Moderate conditions yield high rule-based confidence (68-94%) (>60%)
                        sol_pct = round(max(66.0, min(95.0, base_prob * 100.0 - (crit_score - 30) * 0.2)), 1)

                rule_evals.append({
                    "candidate": cand,
                    "rule_sim": r_sim,
                    "solution_pct": sol_pct,
                    "safe": r_sim.get("safe", False)
                })

            rule_evals.sort(key=lambda x: (1 if x["safe"] else 0, x["solution_pct"]), reverse=True)
            best_eval = rule_evals[0]
            chosen_candidate = best_eval["candidate"]
            chosen_sim = best_eval["rule_sim"]
            best_sol_pct = best_eval["solution_pct"]

            if best_sol_pct > 60.0:
                self._log_activity(
                    "RULE_ENGINE",
                    f"Rule-based solution '{chosen_candidate.get('name')}' confidence is {best_sol_pct}% (>60%) — Auto-approving execution without operator pause.",
                    "success"
                )
                validation = {
                    "approved_for_execution": True,
                    "requires_human_approval": False,
                    "decision": "RULE_BASED_AUTO_APPROVED",
                    "reason": f"Simulation failed; rule-based fallback solution cleared with {best_sol_pct}% confidence (> 60% threshold).",
                    "fallback_mode": "RULE_BASED",
                    "solution_pct": best_sol_pct
                }
                self._record_stage(inc_id, "VALIDATOR", validation, attempt)
                self.audit_logger.log(
                    incident_id=inc_id,
                    agent="RULE_ENGINE",
                    action="RULE_BASED_FALLBACK_AUTO_APPROVED",
                    input_data={"candidate": chosen_candidate, "solution_pct": best_sol_pct},
                    output_data=validation,
                    criticality=criticality_ctx
                )
                await self._broadcast({"type": "validation_decision", "timestamp": _now(), "data": {"incident_id": inc_id, "validation": validation}})
                await self._execute_and_verify(inc_id, anomaly, diagnosis, chosen_candidate, chosen_sim, validation, attempt)
                return
            else:
                self._log_activity(
                    "RULE_ENGINE",
                    f"Rule-based solution '{chosen_candidate.get('name')}' confidence is {best_sol_pct}% (<=60%) — Operator approval required.",
                    "warning"
                )
                validation = {
                    "approved_for_execution": False,
                    "requires_human_approval": True,
                    "decision": "AWAITING_HUMAN_APPROVAL",
                    "reason": f"Simulation failed; rule-based solution confidence is {best_sol_pct}% (<= 60% threshold). Operator approval required.",
                    "fallback_mode": "RULE_BASED",
                    "solution_pct": best_sol_pct
                }
                self._record_stage(inc_id, "VALIDATOR", validation, attempt)
                self.audit_logger.log(
                    incident_id=inc_id,
                    agent="RULE_ENGINE",
                    action="RULE_BASED_FALLBACK_APPROVAL_REQUIRED",
                    input_data={"candidate": chosen_candidate, "solution_pct": best_sol_pct},
                    output_data=validation,
                    criticality=criticality_ctx
                )
                eval_candidates = [x["candidate"] for x in rule_evals]
                eval_sims = [x["rule_sim"] for x in rule_evals]
                self.pending_approvals[inc_id] = {
                    "incident_id": inc_id,
                    "anomaly": anomaly,
                    "diagnosis": diagnosis,
                    "candidate": chosen_candidate,
                    "simulation": chosen_sim,
                    "validation": validation,
                    "candidates": eval_candidates,
                    "simulations": eval_sims,
                    "fallback_mode": "RULE_BASED",
                    "solution_pct": best_sol_pct,
                    "attempt": attempt,
                    "requested_at": _now()
                }
                await self._broadcast({
                    "type": "approval_required",
                    "timestamp": _now(),
                    "data": {
                        "incident_id": inc_id,
                        "anomaly_id": inc_id,
                        "severity": anomaly["severity"],
                        "criticality_score": anomaly["criticality_score"],
                        "policy": "OPERATOR_APPROVAL_REQUIRED",
                        "diagnosis": diagnosis,
                        "candidate": chosen_candidate,
                        "simulation": chosen_sim,
                        "candidates": eval_candidates,
                        "simulations": eval_sims,
                        "fallback_mode": "RULE_BASED",
                        "solution_pct": best_sol_pct,
                        "reason": f"Simulation failed → Rule-based solution confidence {best_sol_pct}% (≤ 60% threshold)"
                    }
                })
                return

        # Rank candidates: simulator-safe ones first (lowest risk_score first
        # among those), unsafe ones last. This only reorders -- nothing here
        # decides pass/fail, so it can't loosen the safety gate downstream.
        ranked = sorted(
            zip(candidates, simulations),
            key=lambda cs: (0 if cs[1].get("safe", False) else 1, cs[1].get("risk_score", 100)),
        )

        # -------------------------------------------------------------
        # Step 3b: Baseline Telemetry Check
        # -------------------------------------------------------------
        # Runs after simulation, before the Safety Gate. The Simulator only
        # verifies a candidate resolves ITS OWN anomaly; this stage checks
        # the same predicted post-fix state against every OTHER tracked
        # parameter too, so a fix that's locally safe but breaks something
        # unrelated gets caught here instead of after execution.
        #
        # Try candidates in ranked order and stop at the first one that's
        # also baseline-clean, instead of only ever checking the single
        # top-ranked candidate. A subsystem's candidate list often has more
        # than one legitimate fix (e.g. battery_undervoltage offers both a
        # plain load-shed and an MPPT recalibration) -- if the top-ranked one
        # happens to have a real side effect (confirmed live: MPPT
        # recalibration pushing solar_current_a past its ceiling) while a
        # perfectly good alternative sits right there in the same list, the
        # incident should fall through to that alternative, not stall out
        # asking for oversight on a problem that has an easy fix sitting
        # unused. Only if EVERY candidate fails baseline does this keep the
        # original (best-ranked) choice and let it flow into Safety Gate for
        # human review, exactly as before.
        self._log_activity("BASELINE_CHECK", "Verifying predicted post-fix state against full telemetry baseline...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "BASELINE_CHECK", "incident_id": inc_id, "status": "running", "message": "Checking every tracked parameter, not just the anomaly's own..."}})

        chosen_candidate, chosen_sim = ranked[0]
        baseline = self.baseline_checker.check(
            predicted_state=chosen_sim.get("predicted_state", {}),
            current_telemetry=snap.values,
            anomaly_params=anomaly.get("affected_params", []),
        )
        if not baseline["passed"] and len(ranked) > 1:
            for alt_candidate, alt_sim in ranked[1:]:
                alt_baseline = self.baseline_checker.check(
                    predicted_state=alt_sim.get("predicted_state", {}),
                    current_telemetry=snap.values,
                    anomaly_params=anomaly.get("affected_params", []),
                )
                if alt_baseline["passed"]:
                    self._log_activity(
                        "BASELINE_CHECK",
                        f"'{chosen_candidate.get('name')}' failed baseline ({baseline['reasoning']}) — "
                        f"falling back to '{alt_candidate.get('name')}', which passes.",
                        "info"
                    )
                    chosen_candidate, chosen_sim, baseline = alt_candidate, alt_sim, alt_baseline
                    break

        self._record_stage(inc_id, "BASELINE_CHECK", baseline, attempt)
        self._log_activity(
            "BASELINE_CHECK",
            f"{baseline['checked_count'] - baseline['violation_count']}/{baseline['checked_count']} parameters pass — {baseline['reasoning']}",
            "success" if baseline["passed"] else "warning"
        )

        self.audit_logger.log(
            incident_id=inc_id,
            agent="BASELINE_CHECK",
            action="TELEMETRY_BASELINE_VERIFIED",
            input_data={"predicted_state": chosen_sim.get("predicted_state", {})},
            output_data=baseline,
            criticality=criticality_ctx
        )

        await self._broadcast({"type": "baseline_check_complete", "timestamp": _now(), "data": {"incident_id": inc_id, "baseline_check": baseline}})

        # -------------------------------------------------------------
        # Step 4: Deterministic Validator / Safety Gate
        # -------------------------------------------------------------
        criticality_eval = {
            "severity": anomaly.get("severity", "LOW"),
            "criticality_score": anomaly.get("criticality_score", 50),
            "policy": anomaly.get("criticality_policy", "AUTO_APPROVED")
        }

        validation = self.validator.validate_action(
            candidate_action=chosen_candidate,
            simulation_result=chosen_sim,
            criticality_eval=criticality_eval,
            current_telemetry=snap.values,
            is_human_authorized=False,
            baseline_check=baseline,
        )
        self._record_stage(inc_id, "VALIDATOR", validation, attempt)

        self.audit_logger.log(
            incident_id=inc_id,
            agent="VALIDATOR",
            action="SAFETY_GATE_EVALUATION",
            input_data={"candidate": chosen_candidate, "simulation": chosen_sim},
            output_data=validation,
            criticality=criticality_ctx
        )

        await self._broadcast({"type": "validation_decision", "timestamp": _now(), "data": {"incident_id": inc_id, "validation": validation}})

        # -------------------------------------------------------------
        # Step 5: Routing (Auto-Execute vs Human Oversight/Approval)
        # -------------------------------------------------------------
        if validation["approved_for_execution"]:
            self._log_activity("SAFETY_GATE", f"Auto-approval cleared for {anomaly['severity']} ({validation['decision']})", "success")
            await self._execute_and_verify(inc_id, anomaly, diagnosis, chosen_candidate, chosen_sim, validation, attempt)
        else:
            if validation["requires_human_approval"] or validation["decision"] == "AWAITING_HUMAN_APPROVAL":
                self._log_activity("SAFETY_GATE", f"CRITICAL incident {inc_id} requires explicit HUMAN APPROVAL", "warning")
            else:
                self._log_activity("SAFETY_GATE", f"HIGH severity incident {inc_id} requires OPERATOR OVERSIGHT", "warning")

            self.pending_approvals[inc_id] = {
                "incident_id": inc_id,
                "anomaly": anomaly,
                "diagnosis": diagnosis,
                "candidate": chosen_candidate,
                "simulation": chosen_sim,
                "validation": validation,
                "baseline_check": baseline,
                "candidates": candidates,
                "simulations": simulations,
                "attempt": attempt,
                "requested_at": _now()
            }

            await self._broadcast({
                "type": "approval_required",
                "timestamp": _now(),
                "data": {
                    "incident_id": inc_id,
                    "anomaly_id": inc_id,
                    "severity": anomaly["severity"],
                    "criticality_score": anomaly["criticality_score"],
                    "policy": anomaly["criticality_policy"],
                    "diagnosis": diagnosis,
                    "candidate": chosen_candidate,
                    "simulation": chosen_sim,
                    "candidates": candidates,
                    "simulations": simulations
                }
            })

    async def approve_procedure(self, anomaly_id: str, action_id: Optional[str] = None) -> bool:
        """Called when human operator approves procedure on dashboard."""
        if anomaly_id not in self.pending_approvals:
            return False

        ctx = self.pending_approvals.pop(anomaly_id)
        chosen_candidate = ctx["candidate"]
        chosen_sim = ctx["simulation"]
        baseline = ctx.get("baseline_check")

        # If operator selected a specific alternative candidate, its baseline
        # check has to be re-run for THAT candidate's predicted state — the
        # stored one was only ever computed for the originally-recommended
        # candidate, and reusing it here would validate the wrong action.
        if action_id:
            for c, s in zip(ctx["candidates"], ctx["simulations"]):
                if c.get("action_id") == action_id:
                    chosen_candidate = c
                    chosen_sim = s
                    baseline = self.baseline_checker.check(
                        predicted_state=s.get("predicted_state", {}),
                        current_telemetry=self.current_telemetry,
                        anomaly_params=ctx["anomaly"].get("affected_params", []),
                    )
                    self._record_stage(anomaly_id, "BASELINE_CHECK", baseline, ctx.get("attempt", 1))
                    break

        self._log_activity("OPERATOR", f"Human operator authorized action '{chosen_candidate.get('name')}' for {anomaly_id}", "success")

        # Re-validate with is_human_authorized = True. Hard safety checks
        # (whitelist, unsafe simulation, risk ceiling, critical telemetry floors)
        # remain absolute blocks. Baseline side-effect warnings are SOFT and are
        # waived by operator authorization — the validator records them as waived.
        validation = self.validator.validate_action(
            candidate_action=chosen_candidate,
            simulation_result=chosen_sim,
            criticality_eval={"severity": ctx["anomaly"]["severity"], "criticality_score": ctx["anomaly"]["criticality_score"]},
            current_telemetry=self.current_telemetry,
            is_human_authorized=True,
            baseline_check=baseline,
        )
        self._record_stage(anomaly_id, "VALIDATOR", validation, ctx.get("attempt", 1))

        self.audit_logger.log(
            incident_id=anomaly_id,
            agent="OPERATOR",
            action="HUMAN_APPROVAL_GRANTED",
            output_data={"action_id": chosen_candidate.get("action_id"), "authorized": True},
            criticality={"score": ctx["anomaly"].get("criticality_score"), "severity": ctx["anomaly"].get("severity")}
        )

        # Human authorization overrides the criticality-based oversight
        # requirement — it does NOT override a hard deterministic violation
        # (whitelist, simulation safety, or baseline check). If re-validation
        # still fails here, executing anyway and letting Post-Monitor
        # potentially observe an unrelated, coincidental telemetry recovery
        # would report "RECOVERED" on an incident where nothing was actually
        # sent to the spacecraft. Stop here instead.
        if not validation.get("approved_for_execution", False):
            subsys = ctx["anomaly"].get("primary_subsystem")
            if subsys:
                self.subsystems_in_flight.discard(subsys)
            reasons = "; ".join(validation.get("violations", [])) or "deterministic safety check failed"
            self._log_activity(
                "SAFETY_GATE",
                f"Operator approval for {anomaly_id} could not be honored — {reasons}",
                "error"
            )
            await self._broadcast({
                "type": "approval_decision",
                "timestamp": _now(),
                "data": {
                    "incident_id": anomaly_id, "anomaly_id": anomaly_id, "approved": False,
                    "reason": f"Blocked by deterministic safety gate even after operator approval: {reasons}"
                }
            })
            return False

        await self._broadcast({"type": "approval_decision", "timestamp": _now(), "data": {"incident_id": anomaly_id, "anomaly_id": anomaly_id, "approved": True}})

        # Route through the same single-worker queue as everything else, so an
        # operator approving one incident can never run concurrently with
        # whatever pipeline the worker already has in flight.
        subsys = ctx["anomaly"].get("primary_subsystem")
        if subsys:
            self.subsystems_in_flight.add(subsys)
        await self.pipeline_queue.put((
            anomaly_id,
            functools.partial(
                self._run_verify_job, subsys,
                anomaly_id, ctx["anomaly"], ctx["diagnosis"], chosen_candidate, chosen_sim, validation, ctx["attempt"]
            )
        ))
        return True

    async def _run_verify_job(self, subsys: Optional[str], *args):
        """Same in-flight discipline as _run_pipeline_job (see its docstring):
        _execute_and_verify's own retry loop can land the incident back in
        pending_approvals (e.g. attempt #2 also needs oversight) rather than
        reaching a terminal state. Releasing the subsystem unconditionally
        here -- as this used to -- let the watcher re-detect the same still-
        unresolved fault as a brand new incident while the first one was
        still sitting in the approval queue, confirmed live: a single hard-
        to-clear battery_undervoltage produced three simultaneous pending
        approvals (041812, 041813, 041814) for what was one physical fault.
        """
        inc_id = args[0] if args else None
        try:
            await self._execute_and_verify(*args)
        finally:
            if subsys and inc_id not in self.pending_approvals:
                self.subsystems_in_flight.discard(subsys)

    async def reject_procedure(self, anomaly_id: str, reason: str = "Operator rejected proposed action") -> bool:
        """Called when human operator denies procedure."""
        if anomaly_id not in self.pending_approvals:
            return False
        ctx = self.pending_approvals.pop(anomaly_id)
        # This incident is now terminal (denied, not retried) — release the
        # subsystem so a genuinely new fault there can be detected. Without
        # this it stayed marked in-flight forever: _run_pipeline_job only
        # clears it once the incident leaves pending_approvals, and nothing
        # else was popping it out of the rejected path.
        subsys = ctx["anomaly"].get("primary_subsystem")
        if subsys:
            self.subsystems_in_flight.discard(subsys)
        self._log_activity("OPERATOR", f"Operator DENIED action for {anomaly_id}: {reason}", "warning")
        self.audit_logger.log(
            incident_id=anomaly_id,
            agent="OPERATOR",
            action="HUMAN_APPROVAL_DENIED",
            output_data={"reason": reason},
            criticality={"score": ctx["anomaly"].get("criticality_score"), "severity": ctx["anomaly"].get("severity")}
        )
        await self._broadcast({"type": "approval_decision", "timestamp": _now(), "data": {"incident_id": anomaly_id, "anomaly_id": anomaly_id, "approved": False, "reason": reason}})
        return True

    async def _execute_and_verify(self, inc_id: str, anomaly: dict, diagnosis: dict, candidate: dict, simulation: dict, validation: dict, attempt: int):
        """Executes authorized procedure, monitors post-execution state, and loops if necessary."""
        criticality_ctx = {"score": anomaly.get("criticality_score"), "severity": anomaly.get("severity")}

        # -------------------------------------------------------------
        # Step 6: Final Command Executor
        # -------------------------------------------------------------
        self._log_activity("EXECUTOR", f"Dispatching whitelisted commands for action '{candidate.get('name')}'...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "EXECUTOR", "incident_id": inc_id, "status": "running", "message": "Transmitting command sequence to spacecraft..."}})

        exec_res = self.executor.execute_action(inc_id, candidate, authorized=validation.get("approved_for_execution", True))
        self._record_stage(inc_id, "EXECUTOR", exec_res, attempt)

        self.audit_logger.log(
            incident_id=inc_id,
            agent="EXECUTOR",
            action="COMMAND_SEQUENCE_EXECUTED",
            input_data={"commands": candidate.get("commands")},
            output_data=exec_res,
            execution_result=exec_res,
            criticality=criticality_ctx
        )

        await self._broadcast({"type": "command_executed", "timestamp": _now(), "data": {"incident_id": inc_id, "execution": exec_res}})

        # Await small telemetry settling duration
        await asyncio.sleep(2.0)

        # -------------------------------------------------------------
        # Step 7: Post-Execution Verification
        # -------------------------------------------------------------
        self._log_activity("POST_MONITOR", "Verifying post-execution telemetry against operational bands...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "POST_MONITOR", "incident_id": inc_id, "status": "running", "message": "Assessing telemetry recovery..."}})

        # Check recovery against the union of the LLM-classified affected_params
        # AND the original, deterministically-detected violation params. The
        # LLM's affected_params list is a hallucination surface — if it under-
        # reports which parameters were actually out of band, checking only
        # its list could declare "recovered" while a real violation persists.
        original_params = [v.get("param") for v in anomaly.get("violations", []) if v.get("param")]
        check_params = list(dict.fromkeys([*anomaly.get("affected_params", []), *original_params]))

        post_res = self.post_monitor.evaluate_recovery(
            incident_id=inc_id,
            affected_params=check_params,
            current_telemetry=self.current_telemetry,
            attempt_number=attempt
        )
        self._record_stage(inc_id, "POST_MONITOR", post_res, attempt)

        await self._broadcast({"type": "post_monitor_result", "timestamp": _now(), "data": {"incident_id": inc_id, "result": post_res}})

        # If not recovered and retry attempts remaining, loop back — as a
        # direct await, not a new detached task. This method already runs
        # inside the single pipeline worker (see _pipeline_worker); awaiting
        # here keeps the whole retry chain inside that one worker slot, so a
        # second, unrelated incident can't start mid-retry and interleave its
        # log lines with this one.
        if not post_res["recovered"] and attempt < MAX_RECOVERY_ATTEMPTS:
            self._log_activity("POST_MONITOR", f"Recovery criteria not met on attempt #{attempt}. Cycling back to Identifier for re-diagnosis!", "warning")
            await asyncio.sleep(2.0)
            snap = self.simulator.tick(interval_s=1.0)
            await self._execute_pipeline(anomaly, snap, attempt=attempt + 1)
            return

        outcome = "RECOVERED" if post_res["recovered"] else "UNRESOLVED_ESCALATED"
        if outcome == "RECOVERED":
            self._log_activity("ORCHESTRATOR", f"Incident {inc_id} successfully resolved and verified nominal.", "success")
        else:
            self._log_activity("ORCHESTRATOR", f"Incident {inc_id} unresolved after {attempt} attempt(s); holding in safe configuration.", "warning")

        # -------------------------------------------------------------
        # Step 8: Report Generator & RAG Memory Ingestion
        # -------------------------------------------------------------
        # Trust score: the average of every agent stage's own self-reported
        # trust_score for this incident (across every retry attempt). Not a
        # single agent's guess about the whole pipeline — an aggregate of how
        # confident each stage was in its own piece of the work.
        trail = self.stage_trail.get(inc_id, [])
        overall_trust = round(sum(t["trust_score"] for t in trail) / len(trail)) if trail else None
        await self._broadcast({"type": "trust_score_update", "timestamp": _now(), "data": {"incident_id": inc_id, "overall_trust_score": overall_trust, "stage_trail": trail}})

        # 1. Structured JSON Report
        report = self.report_generator.generate_incident_report(
            incident_id=inc_id,
            anomaly=anomaly,
            diagnosis=diagnosis,
            action=candidate,
            simulation=simulation,
            execution=exec_res,
            outcome=outcome,
            attempts=attempt,
            trust_score=overall_trust,
            stage_trail=trail,
        )

        # 2. Ingest into persistent RAG Episodic Vector Store
        self.rag_memory.store_incident(report)
        self.resolved_incidents.append(report)
        self._log_activity("RAG_MEMORY", f"Incident report embedded & indexed in RAG memory ({inc_id})", "success")

        # 3. Generate Markdown Runbook
        runbook = self.report_generator.generate_runbook_markdown(
            incident_id=inc_id,
            anomaly=anomaly,
            diagnosis=diagnosis,
            action=candidate,
            simulation=simulation,
            execution=exec_res,
            outcome=outcome,
            trust_score=overall_trust,
            stage_trail=trail,
        )
        self.runbooks.append(runbook)
        self.stage_trail.pop(inc_id, None)

        # 4. Final Audit Trail
        self.audit_logger.log(
            incident_id=inc_id,
            agent="REPORT_GENERATOR",
            action="INCIDENT_RESOLVED_AND_INDEXED",
            output_data=report,
            criticality=criticality_ctx,
            final_outcome=outcome
        )

        await self._broadcast({
            "type": "incident_resolved",
            "timestamp": _now(),
            "data": {
                "incident_id": inc_id,
                "anomaly_id": inc_id,
                "outcome": outcome,
                "report": report,
                "runbook": runbook,
                "rag_stats": self.rag_memory.get_stats()
            }
        })
        await self._broadcast({"type": "runbook_ready", "timestamp": _now(), "data": runbook})

    async def inject_anomaly(self, scenario_key: str) -> dict:
        info = self.simulator.inject_anomaly(scenario_key)
        self._log_activity("OPERATOR", f"Injected fault scenario: {scenario_key}", "warning")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "OPERATOR", "message": f"Injected fault: {scenario_key}"}})
        return info

    def set_broadcast_callback(self, cb: Callable):
        self.broadcast_cb = cb

    async def _broadcast(self, message: dict):
        if self.broadcast_cb:
            await self.broadcast_cb(json.dumps(message))

    def _log_activity(self, agent: str, message: str, level: str = "info"):
        entry = {
            "id": str(uuid.uuid4())[:8],
            "agent": agent,
            "message": message,
            "level": level,
            "timestamp": _now()
        }
        self.activity_log.append(entry)
        if len(self.activity_log) > 200:
            self.activity_log.pop(0)
        print(f"[{agent}] {message}", flush=True)

    def _record_stage(self, inc_id: str, agent: str, output: dict, attempt: int = 1):
        """Appends one agent stage's trust_score/reasoning to the incident's
        trail. Tolerant of agents that don't carry these fields (e.g. before
        every agent was updated to emit them) — falls back to a neutral 70
        rather than skewing the average toward 0."""
        self.stage_trail.setdefault(inc_id, []).append({
            "agent": agent,
            "attempt": attempt,
            "trust_score": output.get("trust_score", 70),
            "reasoning": output.get("reasoning") or output.get("reason") or "",
        })

    def get_status(self) -> dict:
        return {
            "timestamp": _now(),
            "llm_mode": self.llm_provider.current_mode,
            "llm_info": self.llm_provider.get_mode_info(),
            "telemetry": self.current_telemetry,
            "orbital": self.simulator.orbital_context(),
            "active_anomalies": self.active_anomalies[-10:],
            "pending_approvals": list(self.pending_approvals.values()),
            "runbooks": [{"filename": r["filename"], "anomaly_id": r["anomaly_id"], "generated_at": r["generated_at"]} for r in self.runbooks],
            "activity_log": self.activity_log[-50:],
            "available_scenarios": list(ANOMALY_SCENARIOS.keys()),
            "rag_stats": self.rag_memory.get_stats(),
            "rolling_stats": self.latest_rolling_stats,
            "pipeline_status": {
                "current_incident_id": self.current_incident_id,
                "queued_incident_count": self.pipeline_queue.qsize(),
            },
        }