"""
Satellite clustering and host election.

Satellites are grouped into clusters of 5-6 that share an orbital plane and a
mission. Each cluster elects a host: the member that aggregates its siblings'
telemetry, correlates events across them, and speaks to the ground on their
behalf.

Why grouping keys on orbit and mission only, when health and situation are also
clustering inputs: orbit and mission are properties of the spacecraft and
change on the timescale of a manoeuvre. Health and situation change every
tick. Re-forming a constellation because a battery dipped for six seconds would
make membership churn continuously and host election meaningless. So orbit and
mission decide *membership*, while health and situation are rolled up as
cluster state and are what host election and cross-member correlation run on.
"""
import math
from typing import Dict, List, Optional

# Grouping tolerances. Two satellites share a plane when their inclination and
# RAAN agree within these bounds; a shell is an altitude band.
INCLINATION_TOL_DEG = 6.0
RAAN_TOL_DEG = 25.0
ALTITUDE_TOL_KM = 120.0        # radius around a group's mean altitude

TARGET_MAX = 6            # a cluster is 5-6 satellites; this is the ceiling

HEALTH_RANK = {"NORMAL": 0, "DEGRADED": 1, "CRITICAL": 2}


def _ang_delta(a: float, b: float) -> float:
    """Smallest separation between two angles, accounting for the wrap at 360."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def _ang_mean(degs: List[float]) -> float:
    """Circular mean. Averaging 359 and 1 arithmetically gives 180, not 0."""
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    if abs(x) < 1e-12 and abs(y) < 1e-12:
        return degs[0] if degs else 0.0
    r = math.degrees(math.atan2(y, x)) % 360.0
    # atan2 can land a hair below zero, and Python's float modulo turns that
    # into 360.0 rather than 0.0, which would render as "RAAN 360 deg".
    return 0.0 if r > 359.999999 else r


def _balanced_chunks(items: List, hi: int = TARGET_MAX) -> List[List]:
    """Split into as-even-as-possible chunks of at most `hi`.

    A group of 7 cannot be cut into pieces of 5-6, so it becomes 4 and 3 rather
    than 6 and 1: a lopsided split leaves a one-satellite "cluster" that has
    nobody to correlate against, which defeats the point of clustering.
    """
    n = len(items)
    if n == 0:
        return []
    if n <= hi:
        return [items]
    count = math.ceil(n / hi)
    base, extra = divmod(n, count)
    out, i = [], 0
    for c in range(count):
        size = base + (1 if c < extra else 0)
        out.append(items[i:i + size])
        i += size
    return out


def _grow_groups(sims: List) -> List[List]:
    """Group satellites that are genuinely close in orbit.

    Deliberately not fixed bins. Quantising altitude into 150 km buckets puts
    two satellites 57 km apart in different clusters whenever a bucket edge
    falls between them - which is exactly what happened to a Starlink at 296 km
    against its plane-mates at 354 km. Growing a group around its own running
    centroid has no edges to fall on, so membership depends on how close the
    satellites actually are rather than where the grid happens to sit.
    """
    if not sims:
        return []

    # RAAN separates planes most sharply, so walk the fleet in that order and
    # let each group accumulate until a satellite no longer fits its centroid.
    ordered = sorted(sims, key=lambda s: (s.INCLINATION_DEG, s.RAAN_DEG, s.ALTITUDE_KM))

    groups: List[List] = []
    for sim in ordered:
        placed = False
        for g in groups:
            inc_c = sum(m.INCLINATION_DEG for m in g) / len(g)
            alt_c = sum(m.ALTITUDE_KM for m in g) / len(g)
            raan_c = _ang_mean([m.RAAN_DEG for m in g])
            if (abs(sim.INCLINATION_DEG - inc_c) <= INCLINATION_TOL_DEG
                    and _ang_delta(sim.RAAN_DEG, raan_c) <= RAAN_TOL_DEG
                    and abs(sim.ALTITUDE_KM - alt_c) <= ALTITUDE_TOL_KM):
                g.append(sim)
                placed = True
                break
        if not placed:
            groups.append([sim])
    return groups


class ClusterManager:
    """Builds clusters over the live fleet and keeps each one's host current."""

    def __init__(self):
        # Sticky host decisions, keyed by cluster id, so a host does not flap
        # between two equally healthy members on every rebuild.
        self._nominal_host: Dict[str, str] = {}
        self._current_host: Dict[str, str] = {}
        self.clusters: List[Dict] = []

    # ------------------------------------------------------------- grouping
    @staticmethod
    def _phase(sim) -> float:
        """Where the satellite sits in its orbit, 0-1. Used as 'nearness'."""
        return (sim._orbit_time % sim.ORBITAL_PERIOD_S) / sim.ORBITAL_PERIOD_S

    def rebuild(self, sims: Dict[str, object]) -> List[Dict]:
        """Regroup the fleet and re-elect hosts. Returns the cluster list."""
        # Mission is the one genuinely categorical criterion, so it partitions.
        by_mission: Dict[str, List] = {}
        for sim in sims.values():
            by_mission.setdefault(sim.mission, []).append(sim)

        clusters = []
        for mission in sorted(by_mission):
            for gi, group in enumerate(_grow_groups(by_mission[mission])):
                # Order by orbital phase so cluster-mates are actually near each
                # other along the track, not merely in the same plane.
                group.sort(key=self._phase)
                for idx, chunk in enumerate(_balanced_chunks(group)):
                    cid = f"CL-{mission[:3].upper()}-{gi + 1}{idx + 1}"
                    clusters.append(self._describe(cid, mission, chunk))

        self.clusters = clusters
        return clusters

    # -------------------------------------------------------------- describe
    def _describe(self, cid: str, mission: str, members: List) -> Dict:
        host_id, nominal_id, temporary, reason = self._elect(cid, members)

        healths = [m.health_state() for m in members]
        worst = max(healths, key=lambda h: HEALTH_RANK[h])
        # Health is scored on the one-minute mean, so it lags a fresh fault by
        # design. A cluster reporting NORMAL while one of its members is
        # visibly carrying an anomaly is misleading, so an active fault floors
        # the rollup at DEGRADED until the mean catches up.
        if worst == "NORMAL" and any(m.get_active_anomaly() for m in members):
            worst = "DEGRADED"

        # Situation rollups: shared fault, shared lighting, shared ground contact.
        faults = [m.get_active_anomaly() for m in members]
        keys = {f["key"] for f in faults if f}
        shared_anomaly = keys.pop() if len(keys) == 1 and sum(1 for f in faults if f) > 1 else None

        eclipsed = sum(1 for m in members if m._in_eclipse())
        station_sets = [set(m.visible_stations()) for m in members]
        common_stations = sorted(set.intersection(*station_sets)) if station_sets else []

        return {
            "cluster_id": cid,
            "mission": mission,
            "size": len(members),
            "host_id": host_id,
            "nominal_host_id": nominal_id,
            "host_is_temporary": temporary,
            "host_reason": reason,
            "members": [
                {
                    "sat_id": m.sat_id,
                    "name": m.name,
                    "health": m.health_state(),
                    "health_score": m.health_score(),
                    "is_host": m.sat_id == host_id,
                    "in_eclipse": m._in_eclipse(),
                    "stations": m.visible_stations(),
                    "anomaly": (m.get_active_anomaly() or {}).get("key"),
                    "phase_pct": round(self._phase(m) * 100, 1),
                }
                for m in members
            ],
            "health": worst,
            "mean_health_score": round(sum(m.health_score() for m in members) / len(members), 1),
            "situation": {
                "shared_anomaly": shared_anomaly,
                "members_in_eclipse": eclipsed,
                "environment": ("ECLIPSE" if eclipsed == len(members)
                                else "SUNLIT" if eclipsed == 0 else "MIXED"),
                "common_stations": common_stations,
                "contact": bool(common_stations),
            },
            "orbit": {
                "inclination_deg": round(sum(m.INCLINATION_DEG for m in members) / len(members), 2),
                "raan_deg": round(_ang_mean([m.RAAN_DEG for m in members]), 2),
                "altitude_km": round(sum(m.ALTITUDE_KM for m in members) / len(members), 1),
            },
        }

    # ----------------------------------------------------------- host election
    def _elect(self, cid: str, members: List):
        """Pick the host, failing over when the nominal host is unfit.

        Fitness is health first; ties break on nearness to the cluster's mean
        orbital phase, so the host is the healthy member sitting most centrally
        in the formation rather than one on its edge.
        """
        ids = {m.sat_id for m in members}
        mean_phase = sum(self._phase(m) for m in members) / len(members)

        def circular_gap(m):
            d = abs(self._phase(m) - mean_phase)
            return min(d, 1.0 - d)          # the orbit wraps at 1.0

        def fitness(m):
            return (-m.health_score(), 1 if m.get_active_anomaly() else 0, circular_gap(m))

        healthy = [m for m in members if m.health_state() == "NORMAL" and not m.get_active_anomaly()]
        best_overall = min(members, key=fitness)

        nominal = self._nominal_host.get(cid)
        if nominal not in ids:
            # First sight of this cluster, or the old nominal host left it.
            nominal = min(healthy, key=fitness).sat_id if healthy else best_overall.sat_id
            self._nominal_host[cid] = nominal

        nominal_sim = next(m for m in members if m.sat_id == nominal)
        fit = nominal_sim.health_state() == "NORMAL" and not nominal_sim.get_active_anomaly()

        if fit:
            self._current_host[cid] = nominal
            return nominal, nominal, False, "nominal host healthy"

        # Edge case from the brief: the host itself is in trouble, so the
        # nearest healthy member stands in until the nominal host recovers.
        stand_in = min(healthy, key=fitness) if healthy else best_overall
        self._current_host[cid] = stand_in.sat_id
        if stand_in.sat_id == nominal:
            return nominal, nominal, False, "no healthier member available"
        why = "nominal host in anomaly" if nominal_sim.get_active_anomaly() else \
              f"nominal host {nominal_sim.health_state().lower()}"
        return stand_in.sat_id, nominal, True, why

    # ------------------------------------------------------------- lookups
    def cluster_of(self, sat_id: str) -> Optional[Dict]:
        for c in self.clusters:
            if any(m["sat_id"] == sat_id for m in c["members"]):
                return c
        return None

    def status(self) -> Dict:
        return {
            "count": len(self.clusters),
            "clusters": self.clusters,
        }
