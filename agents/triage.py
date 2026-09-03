"""
The three processing tiers a finding passes through before it reaches an
operator, split out of the orchestrator so each stays readable.

  Stage 1  ONBOARD  - the spacecraft screens its own telemetry through the
                      signal conditioner: instantaneous limit checks plus
                      time-windowed persistence, never a window mean. Pure
                      arithmetic, runs for every satellite every check, and is
                      the gate a transient has to pass.
  Stage 2  CLUSTER  - the cluster host correlates the finding against its
                      siblings and decides whether it is one satellite or the
                      whole formation. Also free.
  Stage 3  GROUND   - mission control's LLM agent pipeline. The only expensive
                      tier, so it is served by one worker draining a priority
                      queue, and it has a deterministic fallback.

Every stage fails open: if a tier raises, the finding is escalated rather than
dropped, because the alternative is silently losing a real fault.
"""
import asyncio
from datetime import datetime, timezone
from typing import Optional

from config import SEVERITY_ORDER, TELEMETRY_PARAMS


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TriageMixin:
    """Staged detection for :class:`MissionOrchestrator`.

    A mixin rather than a separate collaborator: these tiers read the
    orchestrator's fleet, cluster map and broadcast channel throughout, and
    threading all of that through a second object would obscure more than the
    split gains.
    """

    # ================================================================= stage 1
    def _stage1_onboard(self, sim) -> Optional[dict]:
        """On-board screen: does this spacecraft see a breach that has held?

        Cheap, local, and the only gate a transient has to pass. A single
        out-of-limit sample is recorded as a spike and goes no further, which is
        what stops sensor noise waking the whole pipeline.
        """
        try:
            frame = sim.conditioned()
            if frame is None:
                return None

            # Out of limit but not yet persistent. Reported, never escalated:
            # this is precisely the transient the persistence gate exists for.
            transients = sim.transient_count()
            if transients:
                self._stage_counts["spikes_rejected"] += transients
                names = ", ".join(r.name for r in frame.suspect)
                self._log_activity(
                    "STAGE-1", f"[{sim.name}] {transients} transient(s) held below "
                               f"confirmation ({names})", "info")

            if not frame.should_escalate:
                # Episode over: let this spacecraft escalate again next time.
                self._inflight = {k for k in self._inflight if k[0] != sim.sat_id}
                return None

            confirmed = sim.confirmed_violations()
            self._stage_counts["stage1"] += 1
            return {
                "stage": 1,
                "source": "ONBOARD",
                "sat_id": sim.sat_id,
                "sat_name": sim.name,
                "subsystems": sorted({v["subsystem"] for v in confirmed}),
                "violations": confirmed,
                "evidence": sim.evidence(),
                "window_average": sim.window_average(),
                "health_score": sim.health_score(),
                "health_state": sim.health_state(),
            }
        except Exception as e:
            # Fail open: a broken screen must never mask a real fault.
            self._stage_counts["failsafe"] += 1
            print(f"[STAGE-1] {sim.sat_id} screen failed, escalating: {e}", flush=True)
            return {"stage": 1, "source": "ONBOARD-FAILSAFE", "sat_id": sim.sat_id,
                    "sat_name": sim.name, "subsystems": [], "violations": [],
                    "window_average": {}, "health_score": 0, "health_state": "DEGRADED"}

    # ================================================================= stage 2
    def _stage2_cluster(self, sim, finding: dict) -> dict:
        """Host-level correlation: is this one spacecraft, or the formation?

        The cluster host compares the finding against its siblings. A fault the
        whole cluster shares points at a common cause - eclipse, a radiation
        environment, a ground-station outage - and matters more than the same
        reading on a single satellite, so its priority is raised.
        """
        try:
            cluster = self.clusters.cluster_of(sim.sat_id)
            if not cluster:
                finding.update({"stage": 2, "scope": "UNCLUSTERED", "cluster_id": None,
                                "host_id": None, "peers_affected": 0})
                return finding

            subsystems = set(finding["subsystems"])
            peers = 0
            for member in cluster["members"]:
                if member["sat_id"] == sim.sat_id:
                    continue
                peer = self.fleet.get(member["sat_id"])
                if peer is None:
                    continue
                if subsystems & {v["subsystem"] for v in peer.confirmed_violations()}:
                    peers += 1

            others = max(cluster["size"] - 1, 1)
            if peers >= max(1, others // 2):
                scope = "CLUSTER-WIDE"
            elif peers:
                scope = "PARTIAL"
            else:
                scope = "ISOLATED"

            self._stage_counts["stage2"] += 1
            finding.update({
                "stage": 2,
                "scope": scope,
                "cluster_id": cluster["cluster_id"],
                "host_id": cluster["host_id"],
                "host_is_temporary": cluster["host_is_temporary"],
                "peers_affected": peers,
                "cluster_size": cluster["size"],
                "environment": cluster["situation"]["environment"],
                "common_stations": cluster["situation"]["common_stations"],
            })
            state = (scope, peers, tuple(finding["subsystems"]))
            if scope != "ISOLATED" and self._last_scope.get(cluster["cluster_id"]) != state:
                self._last_scope[cluster["cluster_id"]] = state
                self._log_activity(
                    "STAGE-2", f"[{cluster['cluster_id']}] {scope}: {peers + 1}/"
                               f"{cluster['size']} members show "
                               f"{'/'.join(finding['subsystems'])} - correlated by host "
                               f"{self._name_of(cluster['host_id'])}", "warning")
            elif scope == "ISOLATED":
                self._last_scope.pop(cluster["cluster_id"], None)
            return finding
        except Exception as e:
            self._stage_counts["failsafe"] += 1
            print(f"[STAGE-2] correlation failed, escalating uncorrelated: {e}", flush=True)
            finding.update({"stage": 2, "scope": "UNKNOWN", "cluster_id": None,
                            "host_id": None, "peers_affected": 0})
            return finding

    # ================================================================= stage 3
    def _priority_of(self, finding: dict) -> str:
        """Provisional severity, used to order the queue before analysis."""
        worst = "LOW"
        for v in finding.get("violations", []):
            meta = TELEMETRY_PARAMS.get(v["param"], {})
            lo = meta.get("warn_low")
            hi = meta.get("warn_high")
            vmin = meta.get("min", 0.0)
            vmax = meta.get("max", 1.0)
            if v["direction"] == "LOW" and lo is not None:
                frac = (lo - v["value"]) / max(lo - vmin, 1e-6)
            elif v["direction"] == "HIGH" and hi is not None:
                frac = (v["value"] - hi) / max(vmax - hi, 1e-6)
            else:
                frac = 0.0
            level = "CRITICAL" if frac >= 0.6 else "HIGH" if frac >= 0.25 else "MEDIUM"
            if SEVERITY_ORDER.index(level) > SEVERITY_ORDER.index(worst):
                worst = level
        # A formation-wide fault outranks the same reading on one spacecraft.
        if finding.get("scope") == "CLUSTER-WIDE" and worst != "CRITICAL":
            worst = SEVERITY_ORDER[min(SEVERITY_ORDER.index(worst) + 1,
                                       len(SEVERITY_ORDER) - 1)]
        return worst

    async def _enqueue_ground(self, sim, finding: dict):
        """Queue a screened finding for ground analysis, highest severity first."""
        key = (sim.sat_id, tuple(finding.get("subsystems", [])))
        if key in self._inflight:
            return
        priority = self._priority_of(finding)
        finding["priority"] = priority

        self._inflight.add(key)
        self._triage_seq += 1
        # Negated rank so the queue pops CRITICAL before MEDIUM. The sequence
        # number breaks ties in arrival order and keeps the tuple comparable,
        # so comparison never reaches the dict.
        await self._triage.put((
            -SEVERITY_ORDER.index(priority), self._triage_seq, sim.sat_id, finding,
        ))
        self._log_activity(
            "STAGE-3", f"[{sim.name}] {priority} {finding.get('scope', '?')} "
                       f"{'/'.join(finding.get('subsystems') or ['?'])} finding "
                       f"queued for ground analysis (depth {self._triage.qsize()})", "info")

    async def _triage_worker(self):
        """Single consumer of the priority queue - stage 3 is the costly tier."""
        while self.self_running:
            try:
                _, _, sat_id, finding = await self._triage.get()
            except asyncio.CancelledError:
                return
            try:
                sim = self.fleet.get(sat_id)
                if sim is not None:
                    await self._ground_analysis(sim, finding)
            except Exception as e:
                self._stage_counts["failsafe"] += 1
                print(f"[STAGE-3] analysis error for {sat_id}: "
                      f"{type(e).__name__}: {e}", flush=True)
            finally:
                # Deliberately NOT discarding `key` here. It stays until stage 1
                # sees the spacecraft clean, so one fault escalates once.
                self._triage.task_done()

    async def _ground_analysis(self, sim, finding: dict):
        """Mission control tier: the LLM agent pipeline, with a hard fallback."""
        self._stage_counts["stage3"] += 1
        snap = self._last_snap.get(sim.sat_id)
        if snap is None:
            return

        self.self_monitor_busy.add(sim.sat_id)
        try:
            history = {p: sim.get_history(p) for p in snap.values}
            try:
                anomaly = await asyncio.wait_for(
                    self.self_monitor.analyze(snap, history), timeout=35.0)
            except Exception as e:
                # Failsafe: stages 1 and 2 already agreed something is wrong, so
                # a model that times out must not make the finding disappear.
                self._stage_counts["failsafe"] += 1
                print(f"[STAGE-3] monitor unavailable ({type(e).__name__}); "
                      f"using deterministic verdict", flush=True)
                anomaly = self._failsafe_anomaly(sim, finding)

            if not anomaly or anomaly.get("severity") == "NOMINAL":
                return

            anomaly["sat_id"] = sim.sat_id
            anomaly["sat_name"] = sim.name
            anomaly["cluster_id"] = finding.get("cluster_id")
            anomaly["scope"] = finding.get("scope")
            anomaly["queue_priority"] = finding.get("priority")

            if self._is_duplicate_anomaly(anomaly):
                return

            self.self_active_anomalies.append(anomaly)
            self._log_activity(
                "MONITOR", f"[{sim.name}] Anomaly detected [{anomaly['severity']}]: "
                           f"{anomaly['summary']}", anomaly["severity"].lower())
            await self._broadcast({"type": "anomaly_detected", "timestamp": _now(),
                                   "data": anomaly})
            await self._handle_anomaly(sim, anomaly, snap)
        finally:
            self.self_monitor_busy.discard(sim.sat_id)

    def _failsafe_anomaly(self, sim, finding: dict) -> dict:
        """Deterministic stand-in for the monitor agent when it cannot answer."""
        subs = finding.get("subsystems") or ["UNKNOWN"]
        params = ", ".join(v["param"] for v in finding.get("violations", [])) or "telemetry"
        return {
            "id": f"ANO-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
            "severity": finding.get("priority", "MEDIUM"),
            "primary_subsystem": subs[0],
            "anomaly_type": "threshold_breach",
            "summary": (f"{'; '.join(finding.get('evidence') or [])} "
                        f"on {sim.name} (failsafe verdict - monitor agent "
                        f"unavailable)".strip()
                        if finding.get("evidence")
                        else f"{params} confirmed outside limits on {sim.name} "
                             f"(failsafe verdict - monitor agent unavailable)"),
            "affected_params": [v["param"] for v in finding.get("violations", [])],
            "detected_at": _now(),
            "tick": sim._tick,
            "failsafe": True,
        }
