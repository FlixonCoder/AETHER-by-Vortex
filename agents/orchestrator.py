"""
Mission Orchestrator Agent
"""
import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from config import (
    ANOMALY_SCENARIOS,
    AUTO_APPROVE_MAX_SEVERITY,
    OFFLINE_MODE,
    SEVERITY_ORDER,
    TELEMETRY_INTERVAL_S,
    ANOMALY_CHECK_EVERY_N_TICKS,
)
from telemetry.simulator import SatelliteSimulator

from .monitor import MonitorAgent
from .diagnostics import DiagnosticsAgent
from .recovery_planner import RecoveryPlannerAgent
from .digital_twin import DigitalTwinAgent
from .runbook_generator import RunbookGenerator

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class MissionOrchestrator:
    def __init__(self):
        self.simulator = SatelliteSimulator()
        self.self_monitor = MonitorAgent()
        self.self_diagnostics = DiagnosticsAgent()
        self.self_recovery = RecoveryPlannerAgent()
        self.self_twin = DigitalTwinAgent()
        self.self_runbook = RunbookGenerator()

        self.self_ws_clients: Set[Any] = set()
        self.self_broadcast_cb: Optional[Callable] = None

        self.self_current_telemetry: Dict[str, float] = {}
        self.self_active_anomalies: List[Dict] = []
        self.self_pending_approvals: Dict[str, Dict] = {}
        self.self_activity_log: List[Dict] = []
        self.self_runbooks: List[Dict] = []

        self.self_running = False
        self.self_tick = 0
        # Guards against stacking up monitor calls when the LLM is slower than
        # the telemetry cadence.
        self.self_monitor_busy = False

    async def run(self):
        self.self_running = True
        self._log_activity("ORCHESTRATOR", "Mission ops system online. Telemetry monitoring active.", "info")
        while self.self_running:
            await self._step()
            await asyncio.sleep(TELEMETRY_INTERVAL_S)

    def stop(self):
        self.self_running = False

    async def _step(self):
        self.self_tick += 1
        snap = self.simulator.tick(interval_s=TELEMETRY_INTERVAL_S)
        self.self_current_telemetry = snap.values

        await self._broadcast({
            "type": "telemetry_update",
            "timestamp": _now(),
            "data": {
                "values": snap.values,
                "units": snap.units,
                "orbital": self.simulator.orbital_context(),
                "violations": snap.violations(),
            }
        })

        # Anomaly detection runs as a background task. Awaiting it here would
        # stall the telemetry cadence by the model's full round-trip latency,
        # freezing the dashboard while the monitor thinks.
        if self.self_tick % ANOMALY_CHECK_EVERY_N_TICKS == 0 and not self.self_monitor_busy:
            history = {p: self.simulator.get_history(p) for p in snap.values}
            asyncio.create_task(self._check_for_anomaly(snap, history))

    async def _check_for_anomaly(self, snap, history: dict):
        self.self_monitor_busy = True
        try:
            # Hard deadline on detection. Retries inside the LLM client can
            # otherwise keep the guard held long enough for a short-lived
            # anomaly to expire before it is ever seen.
            try:
                anomaly = await asyncio.wait_for(
                    self.self_monitor.analyze(snap, history), timeout=35.0
                )
            except asyncio.TimeoutError:
                print("[MONITOR] detection timed out; will retry on next tick", flush=True)
                return

            if anomaly and anomaly.get("severity") != "NOMINAL":
                if not self._is_duplicate_anomaly(anomaly):
                    self.self_active_anomalies.append(anomaly)
                    self._log_activity("MONITOR", f"Anomaly detected [{anomaly['severity']}]: {anomaly['summary']}", anomaly["severity"].lower())
                    await self._broadcast({"type": "anomaly_detected", "timestamp": _now(), "data": anomaly})
                    asyncio.create_task(self._handle_anomaly(anomaly, snap))
        except Exception as e:
            # A background task's exception is otherwise never surfaced.
            print(f"[MONITOR] detection error: {type(e).__name__}: {e}", flush=True)
        finally:
            self.self_monitor_busy = False

    def _is_duplicate_anomaly(self, new_anomaly: dict) -> bool:
        for existing in self.self_active_anomalies[-5:]:
            if (existing.get("primary_subsystem") == new_anomaly.get("primary_subsystem") and
                    abs(existing.get("tick", 0) - new_anomaly.get("tick", 0)) < 30):
                return True
        return False

    async def _handle_anomaly(self, anomaly: dict, snap):
        ano_id = anomaly["id"]
        self._log_activity("DIAGNOSTICS", f"Analyzing root cause for {ano_id}...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "DIAGNOSTICS", "anomaly_id": ano_id, "status": "running", "message": "Performing root-cause analysis..."}})

        history = {p: self.simulator.get_history(p) for p in snap.values}
        diagnosis = await self.self_diagnostics.diagnose(anomaly, snap.values, history, self.simulator.orbital_context())
        diagnosis["anomaly_id"] = ano_id

        self._log_activity("DIAGNOSTICS", f"Root cause identified: {diagnosis.get('root_cause', '?')[:100]}...", "success")
        await self._broadcast({"type": "diagnosis_complete", "timestamp": _now(), "data": diagnosis})

        self._log_activity("RECOVERY_PLANNER", f"Generating recovery options for {ano_id}...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "RECOVERY_PLANNER", "anomaly_id": ano_id, "status": "running", "message": "Generating ranked recovery procedures..."}})

        plan = await self.self_recovery.plan(anomaly, diagnosis, snap.values, self.simulator.orbital_context())
        self._log_activity("RECOVERY_PLANNER", f"{len(plan.get('options', []))} recovery options generated", "success")

        self._log_activity("DIGITAL_TWIN", f"Simulating recovery procedures...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "DIGITAL_TWIN", "anomaly_id": ano_id, "status": "running", "message": "Running digital-twin simulation..."}})

        validated = await self.self_twin.validate(plan, snap.values, self.simulator.orbital_context())
        rejected = [option for option in validated.get("validated_options", []) if not option.get("validation", {}).get("go_no_go", False)]
        if rejected:
            self._log_activity("DIGITAL_TWIN", f"Initial validation rejected {len(rejected)} procedure(s); revising with constrained fallback.", "warning")
            await self._broadcast({"type": "validation_failed", "timestamp": _now(), "data": {"anomaly_id": ano_id, "rejected_count": len(rejected), "reason": rejected[0].get("validation", {}).get("operator_notes", "Constraint violation detected")}})
            validated = await self.self_twin.revise_and_validate(validated, snap.values, self.simulator.orbital_context())
            self._log_activity("DIGITAL_TWIN", "Fallback procedure revalidated against current constraints.", "success")
            await self._broadcast({"type": "validation_recovered", "timestamp": _now(), "data": {"anomaly_id": ano_id, "plan": validated}})
        else:
            self._log_activity("DIGITAL_TWIN", "Simulation complete. Outcomes validated.", "success")
        await self._broadcast({"type": "recovery_options", "timestamp": _now(), "data": {"anomaly_id": ano_id, "plan": validated}})

        severity = anomaly.get("severity", "LOW")
        sev_idx = SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else 1
        auto_idx = SEVERITY_ORDER.index(AUTO_APPROVE_MAX_SEVERITY)

        if sev_idx <= auto_idx:
            self._log_activity("APPROVAL_ROUTER", f"[AUTO-APPROVE] Severity {severity} <= {AUTO_APPROVE_MAX_SEVERITY}. Proceeding.", "success")
            await self._broadcast({"type": "approval_decision", "timestamp": _now(), "data": {"anomaly_id": ano_id, "auto_approved": True, "severity": severity}})
            await self._execute_approved(ano_id, anomaly, diagnosis, validated, validated.get("recommended_rank", 1))
        else:
            self._log_activity("APPROVAL_ROUTER", f"[HUMAN REQUIRED] Severity {severity} - operator approval needed.", "warning")
            self.self_pending_approvals[ano_id] = {"anomaly": anomaly, "diagnosis": diagnosis, "validated_plan": validated, "requested_at": _now()}
            await self._broadcast({"type": "approval_required", "timestamp": _now(), "data": {"anomaly_id": ano_id, "severity": severity, "plan": validated, "diagnosis": diagnosis}})

    async def _execute_approved(self, ano_id: str, anomaly: dict, diagnosis: dict, validated: dict, approved_rank: int):
        self._log_activity("RUNBOOK_GENERATOR", f"Writing operator runbook for approved procedure (rank {approved_rank})...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "RUNBOOK_GENERATOR", "anomaly_id": ano_id, "status": "running", "message": "Generating operator runbook..."}})

        runbook = await self.self_runbook.generate(anomaly, diagnosis, validated, approved_rank)
        self.self_runbooks.append(runbook)

        self._log_activity("RUNBOOK_GENERATOR", f"Runbook saved: {runbook['filename']}", "success")
        await self._broadcast({"type": "runbook_ready", "timestamp": _now(), "data": runbook})

    async def approve_procedure(self, anomaly_id: str, approved_rank: int) -> bool:
        if anomaly_id not in self.self_pending_approvals: return False
        ctx = self.self_pending_approvals.pop(anomaly_id)
        self._log_activity("APPROVAL_ROUTER", f"Operator approved procedure rank {approved_rank} for {anomaly_id}", "success")
        await self._broadcast({"type": "approval_decision", "timestamp": _now(), "data": {"anomaly_id": anomaly_id, "auto_approved": False, "approved_rank": approved_rank}})
        asyncio.create_task(self._execute_approved(anomaly_id, ctx["anomaly"], ctx["diagnosis"], ctx["validated_plan"], approved_rank))
        return True

    async def inject_anomaly(self, scenario_key: str) -> dict:
        info = self.simulator.inject_anomaly(scenario_key)
        self._log_activity("OPERATOR", f"Demo anomaly injected: {scenario_key}", "warning")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {"agent": "OPERATOR", "message": f"Demo anomaly injected: {scenario_key}"}})
        return info

    def set_broadcast_callback(self, cb: Callable):
        self.self_broadcast_cb = cb

    async def _broadcast(self, message: dict):
        if self.self_broadcast_cb: await self.self_broadcast_cb(json.dumps(message))

    def _log_activity(self, agent: str, message: str, level: str = "info"):
        entry = {"id": str(uuid.uuid4())[:8], "agent": agent, "message": message, "level": level, "timestamp": _now()}
        self.self_activity_log.append(entry)
        if len(self.self_activity_log) > 200: self.self_activity_log = self.self_activity_log[-200:]
        print(f"[{agent}] {message}")

    def get_status(self) -> dict:
        return {
            "timestamp": _now(), "offline_mode": OFFLINE_MODE, "telemetry": self.self_current_telemetry, "orbital": self.simulator.orbital_context(),
            "active_anomalies": self.self_active_anomalies[-10:], "pending_approvals": list(self.self_pending_approvals.keys()),
            "runbooks": [{"filename": r["filename"], "anomaly_id": r["anomaly_id"], "generated_at": r["generated_at"]} for r in self.self_runbooks],
            "activity_log": self.self_activity_log[-50:], "available_scenarios": list(ANOMALY_SCENARIOS.keys())
        }