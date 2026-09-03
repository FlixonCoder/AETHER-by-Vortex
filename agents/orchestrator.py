"""
Mission Orchestrator Agent
"""
import asyncio
import json
import uuid
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
from telemetry.clustering import ClusterManager
from .triage import TriageMixin, _now

from .monitor import MonitorAgent
from .diagnostics import DiagnosticsAgent
from .recovery_planner import RecoveryPlannerAgent
from .digital_twin import DigitalTwinAgent
from .runbook_generator import RunbookGenerator

class MissionOrchestrator(TriageMixin):
    #: Ceiling on live simulators. Adopting is one click on the orbit map and
    #: there are ~1,300 objects up there; without a cap a demo session quietly
    #: accumulates hundreds of ticking spacecraft.
    MAX_FLEET = 12

    #: Clusters are re-formed on this cadence. Membership keys on orbit and
    #: mission, which barely move, so there is nothing to gain from doing it
    #: every tick — but host fitness does change, so it cannot be one-shot.
    CLUSTER_REBUILD_EVERY_N_TICKS = 5

    def __init__(self):
        # The fleet is keyed by satellite id. LYRA-1 is the mission spacecraft
        # and is always present; anything else is adopted from the orbit map.
        primary = SatelliteSimulator()
        self.fleet: Dict[str, SatelliteSimulator] = {primary.sat_id: primary}
        self.primary_id = primary.sat_id
        self.active_id = primary.sat_id

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
        # the telemetry cadence. Held per spacecraft, so a slow analysis on one
        # satellite does not block detection on another.
        self.self_monitor_busy: Set[str] = set()

        # --- clustering + tiered processing --------------------------------
        self.clusters = ClusterManager()
        self._last_snap: Dict[str, Any] = {}

        # Stage 3 is the only expensive stage, so it is served by one worker
        # draining a priority queue: CRITICAL findings overtake MEDIUM ones
        # instead of waiting behind them for a model round-trip each.
        self._triage: "asyncio.PriorityQueue" = asyncio.PriorityQueue()
        self._triage_seq = 0
        # Keys already escalated for an ONGOING fault episode. Not cleared when
        # the analysis finishes: the conditioner rightly holds CONFIRMED for the
        # whole fault, and stage 1 re-screens every few ticks, so completion-
        # based clearing re-queued one 50 s fault six times and paid for six
        # model round-trips. Cleared when the spacecraft comes clean instead.
        self._inflight: Set[tuple] = set()
        self._stage_counts = {"stage1": 0, "stage2": 0, "stage3": 0,
                              "spikes_rejected": 0, "failsafe": 0}
        # Last correlation reported per cluster, so a standing cluster-wide
        # fault is announced once rather than on every screening pass.
        self._last_scope: Dict[str, tuple] = {}

    @property
    def simulator(self) -> SatelliteSimulator:
        """The mission spacecraft. Kept so existing callers stay valid."""
        return self.fleet[self.primary_id]

    def get_simulator(self, sat_id: Optional[str] = None) -> SatelliteSimulator:
        return self.fleet.get(sat_id or self.active_id) or self.fleet[self.primary_id]

    def adopt_satellite(self, sat_id: str, name: str, norad_id: Optional[str] = None,
                        altitude_km: Optional[float] = None,
                        inclination_deg: Optional[float] = None,
                        period_min: Optional[float] = None,
                        mission: str = "imaging",
                        raan_deg: Optional[float] = None) -> dict:
        """Bring a satellite picked on the orbit map under mission control.

        Real objects have orbits but no downlink we can read, so adopting one
        starts a simulator seeded with its actual TLE-derived geometry. Its
        telemetry is synthetic; its orbit is not.
        """
        if sat_id in self.fleet:
            return self.fleet[sat_id].identity()

        if len(self.fleet) >= self.MAX_FLEET:
            # Retire the oldest adopted satellite; never the mission spacecraft
            # and never one that is mid-anomaly.
            for key in list(self.fleet):
                if key == self.primary_id or key == self.active_id:
                    continue
                if self.fleet[key].get_active_anomaly():
                    continue
                del self.fleet[key]
                break

        sim = SatelliteSimulator(
            sat_id=sat_id, name=name, norad_id=norad_id,
            altitude_km=altitude_km, inclination_deg=inclination_deg,
            period_s=(period_min * 60.0) if period_min else None,
            mission=mission, raan_deg=raan_deg,
        )
        self.fleet[sat_id] = sim
        self._log_activity("OPERATOR", f"{name} adopted under mission control", "info")
        # Re-form immediately: the operator expects the new satellite to appear
        # in a cluster now, not on whichever tick the rebuild cadence lands.
        try:
            self.clusters.rebuild(self.fleet)
        except Exception as e:
            print(f"[CLUSTER] rebuild after adopt failed: {e}", flush=True)
        return sim.identity()

    def set_active(self, sat_id: str) -> bool:
        if sat_id not in self.fleet:
            return False
        self.active_id = sat_id
        return True

    def fleet_status(self) -> dict:
        return {
            "primary_id": self.primary_id,
            "active_id": self.active_id,
            "satellites": [s.identity() for s in self.fleet.values()],
        }

    async def run(self):
        self.self_running = True
        self._log_activity("ORCHESTRATOR", "Mission ops system online. Telemetry monitoring active.", "info")
        # Stage 3 runs in its own task: draining the queue inline would stall
        # the telemetry cadence for a full model round-trip per finding.
        worker = asyncio.create_task(self._triage_worker())
        try:
            while self.self_running:
                await self._step()
                await asyncio.sleep(TELEMETRY_INTERVAL_S)
        finally:
            worker.cancel()

    def stop(self):
        self.self_running = False

    async def _step(self):
        self.self_tick += 1

        # --- tick the fleet and stream every spacecraft -----------------------
        for sat_id, sim in list(self.fleet.items()):
            snap = sim.tick(interval_s=TELEMETRY_INTERVAL_S)
            self._last_snap[sat_id] = snap
            if sat_id == self.active_id:
                self.self_current_telemetry = snap.values

            cluster = self.clusters.cluster_of(sat_id)
            await self._broadcast({
                "type": "telemetry_update",
                "timestamp": _now(),
                "data": {
                    "sat_id": sat_id,
                    "sat_name": sim.name,
                    "values": snap.values,
                    "units": snap.units,
                    "orbital": sim.orbital_context(),
                    "violations": snap.violations(),
                    # The minute log the brief asks for, alongside the raw frame.
                    "window": {
                        "average": sim.window_average(),
                        "samples": sim.window_samples(),
                        "span_s": sim.window_span_s(),
                    },
                    "health": {"score": sim.health_score(), "state": sim.health_state()},
                    "cluster_id": cluster["cluster_id"] if cluster else None,
                    "cluster_host": cluster["host_id"] if cluster else None,
                    "is_host": bool(cluster and cluster["host_id"] == sat_id),
                }
            })

        # --- keep clusters and their hosts current ---------------------------
        if (self.self_tick % self.CLUSTER_REBUILD_EVERY_N_TICKS == 0
                or not self.clusters.clusters):
            await self._refresh_clusters()

        # --- staged detection -------------------------------------------------
        # Stages 1 and 2 are pure arithmetic, so they run for every spacecraft
        # on every check. Only stage 3 costs a model call, and it is queued.
        if self.self_tick % ANOMALY_CHECK_EVERY_N_TICKS == 0:
            for sat_id, sim in list(self.fleet.items()):
                finding = self._stage1_onboard(sim)
                if not finding:
                    continue
                finding = self._stage2_cluster(sim, finding)
                await self._enqueue_ground(sim, finding)

    async def _refresh_clusters(self):
        """Re-form clusters and announce any host change."""
        previous = {c["cluster_id"]: c["host_id"] for c in self.clusters.clusters}
        try:
            clusters = self.clusters.rebuild(self.fleet)
        except Exception as e:
            self._stage_counts["failsafe"] += 1
            print(f"[CLUSTER] rebuild failed, keeping last topology: {e}", flush=True)
            return

        for c in clusters:
            was = previous.get(c["cluster_id"])
            if was and was != c["host_id"]:
                verb = "failed over to" if c["host_is_temporary"] else "handed back to"
                msg = (f"{c['cluster_id']} host {verb} "
                       f"{self._name_of(c['host_id'])} ({c['host_reason']})")
                self._log_activity("CLUSTER", msg,
                                   "warning" if c["host_is_temporary"] else "success")
                await self._broadcast({"type": "host_change", "timestamp": _now(), "data": c})

        await self._broadcast({"type": "cluster_update", "timestamp": _now(),
                               "data": self.clusters.status()})

    def _name_of(self, sat_id: str) -> str:
        sim = self.fleet.get(sat_id)
        return sim.name if sim else sat_id

    def _is_duplicate_anomaly(self, new_anomaly: dict) -> bool:
        for existing in self.self_active_anomalies[-5:]:
            if (existing.get("sat_id") == new_anomaly.get("sat_id") and
                    existing.get("primary_subsystem") == new_anomaly.get("primary_subsystem") and
                    abs(existing.get("tick", 0) - new_anomaly.get("tick", 0)) < 30):
                return True
        return False

    async def _handle_anomaly(self, sim, anomaly: dict, snap):
        ano_id = anomaly["id"]
        sat = {"sat_id": sim.sat_id, "sat_name": sim.name}
        self._log_activity("DIAGNOSTICS", f"Analyzing root cause for {ano_id}...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {**sat, "agent": "DIAGNOSTICS", "anomaly_id": ano_id, "status": "running", "message": "Performing root-cause analysis..."}})

        history = {p: sim.get_history(p) for p in snap.values}
        diagnosis = await self.self_diagnostics.diagnose(anomaly, snap.values, history, sim.orbital_context())
        diagnosis["anomaly_id"] = ano_id
        diagnosis.update(sat)

        self._log_activity("DIAGNOSTICS", f"Root cause identified: {diagnosis.get('root_cause', '?')[:100]}...", "success")
        await self._broadcast({"type": "diagnosis_complete", "timestamp": _now(), "data": diagnosis})

        self._log_activity("RECOVERY_PLANNER", f"Generating recovery options for {ano_id}...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {**sat, "agent": "RECOVERY_PLANNER", "anomaly_id": ano_id, "status": "running", "message": "Generating ranked recovery procedures..."}})

        plan = await self.self_recovery.plan(anomaly, diagnosis, snap.values, sim.orbital_context())
        self._log_activity("RECOVERY_PLANNER", f"{len(plan.get('options', []))} recovery options generated", "success")

        self._log_activity("DIGITAL_TWIN", f"Simulating recovery procedures...", "info")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(), "data": {**sat, "agent": "DIGITAL_TWIN", "anomaly_id": ano_id, "status": "running", "message": "Running digital-twin simulation..."}})

        validated = await self.self_twin.validate(plan, snap.values, sim.orbital_context())
        rejected = [option for option in validated.get("validated_options", []) if not option.get("validation", {}).get("go_no_go", False)]
        if rejected:
            self._log_activity("DIGITAL_TWIN", f"Initial validation rejected {len(rejected)} procedure(s); revising with constrained fallback.", "warning")
            await self._broadcast({"type": "validation_failed", "timestamp": _now(), "data": {**sat, "anomaly_id": ano_id, "rejected_count": len(rejected), "reason": rejected[0].get("validation", {}).get("operator_notes", "Constraint violation detected")}})
            validated = await self.self_twin.revise_and_validate(validated, snap.values, sim.orbital_context())
            self._log_activity("DIGITAL_TWIN", "Fallback procedure revalidated against current constraints.", "success")
            await self._broadcast({"type": "validation_recovered", "timestamp": _now(), "data": {**sat, "anomaly_id": ano_id, "plan": validated}})
        else:
            self._log_activity("DIGITAL_TWIN", "Simulation complete. Outcomes validated.", "success")
        await self._broadcast({"type": "recovery_options", "timestamp": _now(), "data": {**sat, "anomaly_id": ano_id, "plan": validated}})

        severity = anomaly.get("severity", "LOW")
        sev_idx = SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else 1
        auto_idx = SEVERITY_ORDER.index(AUTO_APPROVE_MAX_SEVERITY)

        if sev_idx <= auto_idx:
            self._log_activity("APPROVAL_ROUTER", f"[AUTO-APPROVE] Severity {severity} <= {AUTO_APPROVE_MAX_SEVERITY}. Proceeding.", "success")
            await self._broadcast({"type": "approval_decision", "timestamp": _now(), "data": {**sat, "anomaly_id": ano_id, "auto_approved": True, "severity": severity}})
            await self._execute_approved(ano_id, anomaly, diagnosis, validated, validated.get("recommended_rank", 1))
        else:
            self._log_activity("APPROVAL_ROUTER", f"[HUMAN REQUIRED] Severity {severity} - operator approval needed.", "warning")
            self.self_pending_approvals[ano_id] = {"anomaly": anomaly, "diagnosis": diagnosis, "validated_plan": validated, "requested_at": _now()}
            await self._broadcast({"type": "approval_required", "timestamp": _now(), "data": {**sat, "anomaly_id": ano_id, "severity": severity, "plan": validated, "diagnosis": diagnosis}})

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

    async def inject_anomaly(self, scenario_key: str, sat_id: Optional[str] = None) -> dict:
        sim = self.get_simulator(sat_id)
        info = sim.inject_anomaly(scenario_key)
        msg = f"Demo anomaly injected on {sim.name}: {scenario_key}"
        self._log_activity("OPERATOR", msg, "warning")
        await self._broadcast({"type": "agent_activity", "timestamp": _now(),
                               "data": {"sat_id": sim.sat_id, "sat_name": sim.name,
                                        "agent": "OPERATOR", "message": msg}})
        return {**info, "sat_id": sim.sat_id, "sat_name": sim.name}

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
            "timestamp": _now(), "offline_mode": OFFLINE_MODE, "telemetry": self.self_current_telemetry, "orbital": self.get_simulator().orbital_context(),
            "fleet": self.fleet_status(),
            "clusters": self.clusters.status(),
            "stages": {**self._stage_counts, "queue_depth": self._triage.qsize()},
            "active_anomalies": self.self_active_anomalies[-10:], "pending_approvals": list(self.self_pending_approvals.keys()),
            "runbooks": [{"filename": r["filename"], "anomaly_id": r["anomaly_id"], "generated_at": r["generated_at"]} for r in self.self_runbooks],
            "activity_log": self.self_activity_log[-50:], "available_scenarios": list(ANOMALY_SCENARIOS.keys())
        }