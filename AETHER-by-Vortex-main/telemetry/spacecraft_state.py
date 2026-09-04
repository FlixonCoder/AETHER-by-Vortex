"""
Causal Physics-Informed Spacecraft State Engine for AETHER.

Architecture (single source of truth):
  SpacecraftState  ←  FaultEngine.step()   (fault modifies state)
        ↓
  RecoveryEngine.apply()  ←  executor recovery commands
        ↓
  SpacecraftPhysics.compute_telemetry()   (state → correlated telemetry)
        ↓
  TelemetrySnapshot → RollingAnalyzer → Agents

Key principle: fault modifies spacecraft state → physics calculates consequences →
telemetry reflects those consequences. Recovery modifies state → physics recalculates
→ telemetry naturally recovers.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Dict, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Physical Parameters
# ─────────────────────────────────────────────────────────────────────────────

ORBITAL_PERIOD_S: float = 5400.0        # 90-min LEO period
ECLIPSE_FRACTION: float = 0.35          # 35% of orbit in eclipse

# EPS
SOLAR_NOMINAL_CURRENT_A: float = 6.2   # peak solar array current (A) at 100% efficiency
SOLAR_NOMINAL_VOLTAGE_V: float = 29.5  # solar array open-circuit voltage
MPPT_EFFICIENCY: float = 0.95          # MPPT converter efficiency
BATTERY_CAPACITY_AH: float = 5.8       # scaled for demo (real ~20 Ah, scaled 3.5x)
BATTERY_NOMINAL_SOC: float = 85.0      # nominal operational SOC (%)
BATTERY_NOMINAL_R_OHM: float = 0.08    # nominal internal resistance (Ω)
BUS_NOMINAL_LOAD_W: float = 45.0       # nominal total bus load (W)

# ADCS
RW_MAX_SPEED_RPM: float = 6000.0
RW_NOMINAL_SPEED_RPM: float = 2000.0
GYRO_NOISE_DPS: float = 0.003          # gyroscope noise floor

# COMMS
NOMINAL_RSSI_DBM: float = -85.0
NOMINAL_SNR_DB: float = 28.0

# Thermal
ORBITAL_HEAT_AMPLITUDE: float = 10.0   # ΔT from orbital thermal cycle (°C)


# ─────────────────────────────────────────────────────────────────────────────
# SpacecraftState — Single Authoritative State
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SpacecraftState:
    """
    Single authoritative spacecraft state. All telemetry is DERIVED from this.
    Never directly set telemetry — always modify state, then derive telemetry.
    """

    # ── Environment / Orbit ──
    orbit_time_s: float = 0.0
    sun_exposure: float = 1.0       # 0.0 = full eclipse, 1.0 = full sunlight
    eclipse: bool = False

    # ── EPS: Battery ──
    battery_soc: float = BATTERY_NOMINAL_SOC    # State of Charge (%)
    battery_health: float = 1.0                 # 0-1 (degrades with fault/aging)
    battery_temp_c: float = 20.0                # Battery pack temperature (°C)
    battery_internal_resistance: float = BATTERY_NOMINAL_R_OHM  # Ω

    # ── EPS: Solar ──
    solar_efficiency: float = 1.0               # 0-1 (degrades with solar fault)
    solar_string_health: float = 1.0            # 0-1 (string open-circuit health)

    # ── EPS: Bus ──
    bus_load_w: float = BUS_NOMINAL_LOAD_W      # Total bus power draw (W)

    # ── ADCS ──
    attitude_error_deg: float = 0.10
    gyro_bias_dps: float = 0.0                  # Gyroscope bias (°/s)
    rw_speed_rpm: float = RW_NOMINAL_SPEED_RPM
    external_disturbance_torque_nm: float = 0.0 # External torque (Nm)

    # ── OBC ──
    cpu_load_pct: float = 30.0
    memory_used_pct: float = 45.0
    memory_error_count: int = 0
    watchdog_count: int = 0

    # ── COMMS ──
    rf_attenuation_db: float = 0.0              # Additional RF path loss (dB)
    rf_intermittent: bool = False               # Intermittent failure flag

    # ── Navigation ──
    gps_fix: bool = True
    gps_satellites: int = 8
    gps_recovery_ticks: int = 0                 # Countdown for cold-start TTFF

    # ── Thermal (subsystem temps) ──
    obc_temp_c: float = 25.0
    payload_temp_c: float = 22.0
    radio_temp_c: float = 30.0

    # ── Fault State ──
    active_fault: Optional[str] = None
    fault_progression: float = 0.0              # 0.0 → 1.0 over fault lifetime
    fault_tick: int = 0
    fault_duration_ticks: int = 0
    fault_metadata: dict = field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────────
# Battery Physics Model
# ─────────────────────────────────────────────────────────────────────────────

def battery_ocv(soc_pct: float, health: float = 1.0) -> float:
    """
    Open-circuit voltage as function of SOC and health — piecewise-linear approximation
    of Li-Ion discharge curve scaled to spacecraft bus voltage:
      SOC   0%  → 22.0 V
      SOC  20%  → 23.5 V
      SOC  80%  → 28.0 V   (nominal operating point)
      SOC 100%  → 33.6 V
    Health factor models cell short/bypass/degradation lowering terminal voltage.
    """
    s = max(0.0, min(100.0, soc_pct))
    if s < 20.0:
        base = 22.0 + (s / 20.0) * 1.5             # 22.0 → 23.5 V (deep discharge)
    elif s < 80.0:
        base = 23.5 + ((s - 20.0) / 60.0) * 4.5   # 23.5 → 28.0 V (nominal region)
    else:
        base = 28.0 + ((s - 80.0) / 20.0) * 5.6   # 28.0 → 33.6 V (high SOC)
    hf = 0.75 + 0.25 * max(0.0, min(1.0, health))
    return base * hf



def effective_internal_resistance(r_base: float, temp_c: float, health: float) -> float:
    """
    Internal resistance increases at extreme temperatures and with degraded health.
    Temperature derating: below 10°C or above 35°C.
    Health derating: 1.0 = nominal, 0.0 → very high resistance.
    """
    # Temperature effect
    if temp_c < 10.0:
        temp_factor = 1.0 + (10.0 - temp_c) * 0.06
    elif temp_c > 35.0:
        temp_factor = 1.0 + (temp_c - 35.0) * 0.04
    else:
        temp_factor = 1.0
    # Health degradation effect
    health_factor = 1.0 + (1.0 - max(0.05, health)) * 2.5
    return r_base * temp_factor * health_factor


# ─────────────────────────────────────────────────────────────────────────────
# Orbital Model
# ─────────────────────────────────────────────────────────────────────────────

class OrbitalModel:
    """Simplified LEO orbital model: sun exposure, eclipse, ground track."""

    INCLINATION_DEG = 51.6
    ALTITUDE_KM = 513.7
    EARTH_ROTATION_DEG_PER_S = 360.0 / 86164.0

    def update(self, state: SpacecraftState, dt_s: float) -> None:
        """Advance orbit time; update sun_exposure and eclipse flag."""
        state.orbit_time_s = (state.orbit_time_s + dt_s) % ORBITAL_PERIOD_S
        eclipse_start_s = ORBITAL_PERIOD_S * (1.0 - ECLIPSE_FRACTION)
        state.eclipse = state.orbit_time_s >= eclipse_start_s

        if state.eclipse:
            state.sun_exposure = 0.0
        else:
            # Full sun exposure in sunlit phase with solar array sun-tracking
            state.sun_exposure = 1.0

    def ground_track(self, orbit_time_s: float) -> Dict[str, float]:
        """Return sub-satellite point for current orbit time."""
        inc = math.radians(self.INCLINATION_DEG)
        u = 2.0 * math.pi * (orbit_time_s / ORBITAL_PERIOD_S)
        lat = math.asin(math.sin(inc) * math.sin(u))
        dlon = math.atan2(math.cos(inc) * math.sin(u), math.cos(u))
        lon = math.degrees(dlon) - self.EARTH_ROTATION_DEG_PER_S * orbit_time_s
        lon = ((lon + 180.0) % 360.0) - 180.0
        return {
            "latitude": round(math.degrees(lat), 4),
            "longitude": round(lon, 4),
            "altitude_km": self.ALTITUDE_KM,
            "inclination_deg": self.INCLINATION_DEG,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Fault Scenario Metadata (for orchestrator/API display, no deltas)
# ─────────────────────────────────────────────────────────────────────────────

FAULT_SCENARIOS: Dict[str, Dict] = {
    # ── 10 primary anomaly scenarios ──
    "battery_undervoltage": {
        "subsystem": "EPS", "severity": "LOW",
        "description": "Aged battery with high internal resistance under load causing bus undervoltage",
        "duration_ticks": 60, "failure_mode": "load_induced",
    },
    "solar_array_degradation": {
        "subsystem": "EPS", "severity": "MEDIUM",
        "description": "Progressive solar array string open-circuit failure reducing solar generation",
        "duration_ticks": 90, "failure_mode": "gradual",
    },
    "rw_saturation": {
        "subsystem": "ADCS", "severity": "MEDIUM",
        "description": "External disturbance torque accumulating reaction wheel angular momentum toward saturation",
        "duration_ticks": 75, "failure_mode": "gradual",
    },
    "gyro_drift": {
        "subsystem": "ADCS", "severity": "MEDIUM",
        "description": "Gyroscope bias drift causing attitude estimation and pointing error accumulation",
        "duration_ticks": 80, "failure_mode": "gradual",
    },
    "battery_overtemperature": {
        "subsystem": "THERMAL", "severity": "HIGH",
        "description": "Heater control relay stuck closed causing battery thermal runaway",
        "duration_ticks": 60, "failure_mode": "gradual",
    },
    "obc_memory_overflow": {
        "subsystem": "OBC", "severity": "MEDIUM",
        "description": "Payload buffer memory leak saturating OBC heap and CPU",
        "duration_ticks": 70, "failure_mode": "sudden",
    },
    "comms_degradation": {
        "subsystem": "COMMS", "severity": "CRITICAL",
        "description": "Antenna gimbal mechanical binding causing link margin collapse",
        "duration_ticks": 60, "failure_mode": "sudden_intermittent",
    },
    "gps_loss": {
        "subsystem": "NAV", "severity": "HIGH",
        "description": "GPS receiver failure causing navigation fix loss",
        "duration_ticks": 90, "failure_mode": "sudden",
    },
    "power_bus_overcurrent": {
        "subsystem": "EPS", "severity": "HIGH",
        "description": "Payload subsystem fault causing bus overcurrent and accelerated battery discharge",
        "duration_ticks": 50, "failure_mode": "load_induced",
    },
    "solar_thermal_excursion": {
        "subsystem": "THERMAL", "severity": "HIGH",
        "description": "Attitude anomaly increasing solar heat absorption causing subsystem thermal excursion",
        "duration_ticks": 70, "failure_mode": "environmental",
    },
    # ── Legacy scenario aliases (backward compatibility) ──
    "attitude_drift": {
        "subsystem": "ADCS", "severity": "MEDIUM",
        "description": "Reaction wheel friction increase causing attitude control degradation (→ rw_saturation)",
        "duration_ticks": 60, "failure_mode": "gradual",
    },
    "memory_overflow": {
        "subsystem": "OBC", "severity": "MEDIUM",
        "description": "Runaway payload data buffer causing memory exhaustion (→ obc_memory_overflow)",
        "duration_ticks": 60, "failure_mode": "sudden",
    },
    "thermal_excursion": {
        "subsystem": "THERMAL", "severity": "HIGH",
        "description": "Heater controller malfunction causing thermal runaway (→ battery_overtemperature)",
        "duration_ticks": 60, "failure_mode": "gradual",
    },
    "comms_loss": {
        "subsystem": "COMMS", "severity": "CRITICAL",
        "description": "Antenna pointing mechanism failure (→ comms_degradation)",
        "duration_ticks": 60, "failure_mode": "sudden_intermittent",
    },
    "ollama_failure_test": {
        "subsystem": "OBC", "severity": "HIGH",
        "description": "Demo fault: Local AI fallback test (→ obc_memory_overflow)",
        "duration_ticks": 60, "failure_mode": "sudden",
    },
    "full_offline_test": {
        "subsystem": "ADCS", "severity": "CRITICAL",
        "description": "Demo fault: Deterministic Rule Engine fallback test (→ rw_saturation)",
        "duration_ticks": 60, "failure_mode": "gradual",
    },
}


# ─────────────────────────────────────────────────────────────────────────────
# Fault Engine — Causal State Modification per Fault Type
# ─────────────────────────────────────────────────────────────────────────────

class FaultEngine:
    """
    Applies per-tick fault progression to SpacecraftState.

    Principle: Each fault modifies SpacecraftState fields that represent
    physical root causes. SpacecraftPhysics then derives affected telemetry.
    Telemetry is NEVER directly manipulated here.
    """

    def inject(self, state: SpacecraftState, fault_key: str, duration_ticks: int) -> None:
        """Inject a fault — draws a random magnitude within each fault's
        calibrated range and sets initial fault state conditions.

        Every injection of the same scenario_key previously produced the
        exact same fixed numbers every time (e.g. comms_loss always set
        rf_attenuation_db = 23.0 flat) -- so the RAG memory that's supposed to
        be learning from incidents saw literally identical "different"
        incidents forever, and severity was pinned to whatever one point on
        the scale that fixed constant happened to land on. Each branch below
        now draws from a range and stores the drawn target(s) in
        fault_metadata, which step() reads back every tick instead of its own
        hardcoded literal -- so the randomness actually persists through the
        fault's ramp instead of being overwritten on the next tick.
        """
        meta = FAULT_SCENARIOS.get(fault_key, {})
        state.active_fault = fault_key
        state.fault_tick = 0
        state.fault_progression = 0.0
        state.fault_duration_ticks = duration_ticks
        state.fault_metadata = {}
        fm = state.fault_metadata

        # ── Immediate initial conditions per fault type ──

        if fault_key == "battery_undervoltage":
            # Load-induced: battery depleted + cell degradation.
            # soc's upper bound is capped at 27 (warn_low is 30) so every
            # draw is a genuine, detectable violation with some margin, not
            # just variety for variety's sake -- a range that spans the warn
            # threshold means some fraction of injections silently produce no
            # real anomaly at all, which reads as a detection failure when
            # it's actually just an under-threshold draw. Confirmed by direct
            # simulation: the old 24-42 range left ~20% of draws never
            # crossing warn_low.
            fm["soc_floor"] = random.uniform(9.0, 15.0)
            state.battery_soc = random.uniform(18.0, 27.0)
            state.battery_internal_resistance = BATTERY_NOMINAL_R_OHM * random.uniform(1.7, 2.9)
            state.battery_health = random.uniform(0.62, 0.87)

        elif fault_key == "solar_array_degradation":
            # Direct solar string loss — battery SOC will naturally drain over time
            fm["eff_start"] = random.uniform(0.20, 0.48)
            fm["eff_end"] = random.uniform(0.14, fm["eff_start"] - 0.04)
            fm["health_start"] = random.uniform(0.20, 0.48)
            fm["health_end"] = random.uniform(0.15, fm["health_start"] - 0.03)
            state.solar_efficiency = fm["eff_start"]
            state.solar_string_health = fm["health_start"]

        elif fault_key in ("rw_saturation", "attitude_drift", "full_offline_test"):
            # External torque accumulates, wheel speed driven beyond threshold.
            # full_offline_test is the one alias of this fault declared
            # CRITICAL — bias its range meaningfully higher than the plain
            # rw_saturation/attitude_drift draws, which share this branch but
            # are declared MEDIUM.
            if fault_key == "full_offline_test":
                fm["rw_start"] = random.uniform(5750.0, 5900.0)
                fm["rw_peak"] = random.uniform(5950.0, RW_MAX_SPEED_RPM)
                fm["att_start"] = random.uniform(3.2, 4.2)
                fm["att_peak"] = random.uniform(5.5, 6.5)
                fm["torque_base"] = random.uniform(0.00042, 0.00055)
            else:
                fm["rw_start"] = random.uniform(5500.0, 5750.0)
                fm["rw_peak"] = random.uniform(5750.0, 5900.0)
                fm["att_start"] = random.uniform(2.4, 3.1)
                fm["att_peak"] = random.uniform(3.4, 5.2)
                fm["torque_base"] = random.uniform(0.00028, 0.00040)
            fm["torque_gain"] = random.uniform(0.00010, 0.00020)
            state.rw_speed_rpm = max(state.rw_speed_rpm, fm["rw_start"])
            state.attitude_error_deg = max(state.attitude_error_deg, fm["att_start"])
            state.external_disturbance_torque_nm = fm["torque_base"]

        elif fault_key == "gyro_drift":
            # Gyro bias accumulation drives attitude pointing error
            fm["bias_start"] = random.uniform(0.22, 0.48)
            fm["bias_peak"] = random.uniform(0.9, 1.4)
            fm["att_start"] = random.uniform(1.9, 2.7)
            fm["att_peak"] = random.uniform(3.6, 5.0)
            state.gyro_bias_dps = fm["bias_start"]
            state.attitude_error_deg = max(state.attitude_error_deg, fm["att_start"])

        elif fault_key in ("battery_overtemperature", "thermal_excursion"):
            # Stuck heater relay heats battery bay and payload
            fm["batt_start"] = random.uniform(46.0, 54.0)
            fm["batt_peak"] = random.uniform(56.0, 66.0)
            fm["payload_start"] = random.uniform(56.0, 64.0)
            fm["payload_peak"] = random.uniform(66.0, 78.0)
            fm["obc_start"] = random.uniform(48.0, 55.0)
            fm["obc_peak"] = random.uniform(56.0, 64.0)
            state.battery_temp_c = max(state.battery_temp_c, fm["batt_start"])
            state.payload_temp_c = max(state.payload_temp_c, fm["payload_start"])
            state.obc_temp_c = max(state.obc_temp_c, fm["obc_start"])

        elif fault_key in ("obc_memory_overflow", "memory_overflow", "ollama_failure_test"):
            # Runaway data buffer leaks memory and saturates CPU
            fm["mem_start"] = random.uniform(80.0, 91.0)
            fm["mem_peak"] = random.uniform(92.0, 99.0)
            fm["cpu_start"] = random.uniform(74.0, 86.0)
            fm["cpu_peak"] = random.uniform(88.0, 98.0)
            state.memory_used_pct = max(state.memory_used_pct, fm["mem_start"])
            state.cpu_load_pct = max(state.cpu_load_pct, fm["cpu_start"])
            state.memory_error_count += random.randint(2, 5)
            state.watchdog_count += 1

        elif fault_key in ("comms_degradation", "comms_loss"):
            # RF antenna gimbal mispointing causes severe path loss. Range is
            # deliberately wide and skewed high: this is one of the two
            # scenarios declared CRITICAL, so its worst draws need to reach a
            # near-total link blackout, not just "degraded".
            fm["atten_mean"] = random.uniform(17.0, 27.0)
            fm["atten_lo"] = random.uniform(6.0, 10.0)
            fm["atten_hi"] = random.uniform(30.0, 38.0)
            state.rf_attenuation_db = random.uniform(fm["atten_mean"] - 3, fm["atten_mean"] + 3)
            state.rf_intermittent = True

        elif fault_key == "gps_loss":
            # Loss of GPS lock and tracking satellites
            state.gps_fix = False
            state.gps_satellites = random.randint(0, 2)
            fm["reacquire_chance"] = random.uniform(0.05, 0.18)

        elif fault_key == "power_bus_overcurrent":
            # Payload short / bus overcurrent accelerates battery discharge
            fm["extra_load_w"] = random.uniform(42.0, 78.0)
            state.bus_load_w = max(state.bus_load_w, 95.0 + fm["extra_load_w"] * 0.17)
            state.fault_metadata["extra_load_w"] = fm["extra_load_w"]

        elif fault_key == "solar_thermal_excursion":
            # Off-nominal attitude increases absorbed solar flux.
            # Targets are asymptotes the step() formula approaches at prog=1
            # scaled by heat_factor (itself <1.7x even at prog=1 outside
            # eclipse) -- the old ranges (e.g. obc_target 38-48, all BELOW
            # warn_high=50) meant some draws could never cross their
            # threshold at all regardless of how long the fault ran, and
            # eclipse timing could suppress the rest. Raised so every draw
            # clears its threshold with real margin once heat_factor ramps
            # up, confirmed by direct simulation.
            fm["att_peak"] = random.uniform(2.2, 3.4)
            fm["heat_mult"] = random.uniform(0.65, 1.05)
            fm["batt_target"] = random.uniform(44.0, 58.0)
            fm["payload_target"] = random.uniform(60.0, 74.0)
            fm["obc_target"] = random.uniform(46.0, 56.0)
            state.attitude_error_deg = max(state.attitude_error_deg, 0.10)
            state.payload_temp_c = max(state.payload_temp_c, 57.0)
            state.obc_temp_c = max(state.obc_temp_c, 51.5)

    def step(self, state: SpacecraftState, dt_s: float) -> None:
        """Advance fault progression by one tick. Modifies state in-place."""
        if state.active_fault is None:
            return

        state.fault_tick += 1
        if state.fault_duration_ticks > 0:
            state.fault_progression = min(1.0, state.fault_tick / state.fault_duration_ticks)
        prog = state.fault_progression
        fault = state.active_fault
        fm = state.fault_metadata or {}

        # ── 1. Battery Undervoltage (load-induced, persistent) ──
        if fault == "battery_undervoltage":
            # High-resistance battery continues to drain under load
            floor = fm.get("soc_floor", 12.0)
            state.battery_soc = max(floor, state.battery_soc - 0.15)
            # Solar efficiency stays 1.0 (nominal)

        # ── 2. Solar Array Degradation (gradual over fault lifetime) ──
        elif fault == "solar_array_degradation":
            eff_start, eff_end = fm.get("eff_start", 0.35), fm.get("eff_end", 0.20)
            hlt_start, hlt_end = fm.get("health_start", 0.35), fm.get("health_end", 0.22)
            state.solar_efficiency = eff_start + (eff_end - eff_start) * prog
            state.solar_string_health = hlt_start + (hlt_end - hlt_start) * prog
            # solar_current_a/solar_power_w have no warn_low (deliberately --
            # both legitimately swing near zero every eclipse, so a raw
            # threshold there would false-alarm every orbit). The only
            # telemetry consequence of reduced generation is slower battery
            # charging, which the ordinary power-balance model in
            # step_state() resolves over many minutes -- far past this
            # fault's ~30-tick window, so on its own this fault was
            # completely undetectable (confirmed by direct simulation: 0/30
            # trials ever crossed any warn threshold). Accelerate SOC drain
            # while the fault is active so it reliably crosses the existing,
            # eclipse-independent battery_soc_pct warn_low(30) within the
            # window, the same pattern battery_undervoltage already uses.
            state.battery_soc = max(5.0, state.battery_soc - 2.3)
            # NOTE: battery_soc drains naturally via power balance — no direct SOC manipulation

        # ── 3. Reaction Wheel Saturation (gradual, disturbance-driven) ──
        elif fault in ("rw_saturation", "attitude_drift", "full_offline_test"):
            torque_base = fm.get("torque_base", 0.00035)
            torque_gain = fm.get("torque_gain", 0.00015)
            rw_start, rw_peak = fm.get("rw_start", 5650.0), fm.get("rw_peak", 5850.0)
            att_start, att_peak = fm.get("att_start", 2.8), fm.get("att_peak", 4.8)
            state.external_disturbance_torque_nm = torque_base + prog * torque_gain
            state.rw_speed_rpm = min(RW_MAX_SPEED_RPM, rw_start + prog * (rw_peak - rw_start))
            state.attitude_error_deg = min(6.5, att_start + prog * (att_peak - att_start) + random.gauss(0, 0.05))

        # ── 4. Gyroscope Drift (gradual bias accumulation) ──
        elif fault == "gyro_drift":
            bias_start, bias_peak = fm.get("bias_start", 0.35), fm.get("bias_peak", 0.85)
            att_start, att_peak = fm.get("att_start", 2.3), fm.get("att_peak", 3.8)
            state.gyro_bias_dps = min(1.4, bias_start + prog * (bias_peak - bias_start))
            state.attitude_error_deg = min(5.0, att_start + prog * (att_peak - att_start) + random.gauss(0, 0.04))

        # ── 5. Battery Overtemperature (gradual, failed heater relay) ──
        elif fault in ("battery_overtemperature", "thermal_excursion"):
            batt_start, batt_peak = fm.get("batt_start", 52.0), fm.get("batt_peak", 56.0)
            pl_start, pl_peak = fm.get("payload_start", 62.0), fm.get("payload_peak", 67.0)
            obc_start, obc_peak = fm.get("obc_start", 53.0), fm.get("obc_peak", 56.0)
            state.battery_temp_c = min(batt_peak, batt_start + prog * (batt_peak - batt_start))
            state.payload_temp_c = min(pl_peak, pl_start + prog * (pl_peak - pl_start))
            state.obc_temp_c = min(obc_peak, obc_start + prog * (obc_peak - obc_start))

        # ── 6. OBC Memory Overflow (sudden start, then gradual fill) ──
        elif fault in ("obc_memory_overflow", "memory_overflow", "ollama_failure_test"):
            mem_peak = fm.get("mem_peak", 96.0)
            cpu_peak = fm.get("cpu_peak", 94.0)
            state.memory_used_pct = min(mem_peak, state.memory_used_pct + 0.35)
            state.cpu_load_pct = min(cpu_peak, state.cpu_load_pct + 0.25)
            if state.memory_used_pct > 85.0:
                state.watchdog_count += 1
                state.memory_error_count += 1

        # ── 7. Communications Degradation (sudden + intermittent fluctuation) ──
        elif fault in ("comms_degradation", "comms_loss"):
            # Intermittent: RF link fluctuates around a degraded mean drawn at
            # injection time, within a range also drawn at injection time.
            mean = fm.get("atten_mean", 18.0)
            lo = fm.get("atten_lo", 8.0)
            hi = fm.get("atten_hi", 32.0)
            if random.random() < 0.35:
                state.rf_attenuation_db = mean + random.gauss(0, 5.0)
            state.rf_attenuation_db = max(lo, min(hi, state.rf_attenuation_db))

        # ── 8. GPS Loss (sudden onset, persistent degraded state) ──
        elif fault == "gps_loss":
            # GPS remains lost; satellite count fluctuates at near-zero
            if random.random() < fm.get("reacquire_chance", 0.12):
                state.gps_satellites = max(0, state.gps_satellites + random.randint(-1, 1))
            state.gps_satellites = min(2, state.gps_satellites)

        # ── 9. Power Bus Overcurrent (sudden, persistent extra load) ──
        elif fault == "power_bus_overcurrent":
            # High discharge current causes gradual battery heating, scaled to
            # how much extra load was drawn at injection.
            temp_ceiling = 38.0 + fm.get("extra_load_w", 60.0) * 0.09
            state.battery_temp_c = min(state.battery_temp_c + 0.12, temp_ceiling)
            # Bus load stays elevated — physics computes high discharge current

        # ── 10. Solar Thermal Excursion (environmental, gradual heating) ──
        elif fault == "solar_thermal_excursion":
            # Attitude anomaly → increased solar incidence → more absorbed heat
            att_peak = fm.get("att_peak", 2.8)
            heat_mult = fm.get("heat_mult", 0.85)
            batt_t = fm.get("batt_target", 46.0)
            pl_t = fm.get("payload_target", 60.0)
            obc_t = fm.get("obc_target", 43.0)
            state.attitude_error_deg = min(att_peak, 0.10 + prog * (att_peak - 0.10) + random.gauss(0, 0.05))
            heat_factor = 1.0 + prog * heat_mult * state.sun_exposure
            state.battery_temp_c += (22.0 + prog * (batt_t - 22.0) * heat_factor - state.battery_temp_c) * 0.045
            state.payload_temp_c += (22.0 + prog * (pl_t - 22.0) * heat_factor - state.payload_temp_c) * 0.05
            state.obc_temp_c += (25.0 + prog * (obc_t - 25.0) * heat_factor - state.obc_temp_c) * 0.03

        # ── Auto-expire at end of natural fault duration ──
        if state.fault_duration_ticks > 0 and state.fault_tick >= state.fault_duration_ticks:
            state.active_fault = None


# ─────────────────────────────────────────────────────────────────────────────
# Recovery Engine — Applies Recovery Commands to SpacecraftState
# ─────────────────────────────────────────────────────────────────────────────

class RecoveryEngine:
    """
    Applies recovery commands to SpacecraftState.

    Recovery modifies underlying state — SpacecraftPhysics then derives
    the naturally recovering telemetry. Not all faults are instantly reversible.
    """

    def apply(self, state: SpacecraftState, cmd_name: str, params: dict = None) -> dict:
        """Apply a recovery command; returns dict describing what changed."""
        params = params or {}

        if cmd_name == "MPPT_RECALIBRATE":
            # Partial recovery of degraded solar efficiency (not instant full restoration)
            old_eff = state.solar_efficiency
            state.solar_efficiency = min(0.96, state.solar_efficiency + 0.38)
            state.solar_string_health = min(0.94, state.solar_string_health + 0.28)
            return {"applied": True, "desc": f"MPPT recalibrated; solar efficiency {old_eff:.2f}→{state.solar_efficiency:.2f}"}

        elif cmd_name == "REACTION_WHEEL_DESAT":
            # Magnetorquer desaturation: unloads angular momentum
            state.external_disturbance_torque_nm = 0.0
            state.rw_speed_rpm = RW_NOMINAL_SPEED_RPM + random.gauss(0, 80)
            state.attitude_error_deg = max(0.08, state.attitude_error_deg - 3.5)
            return {"applied": True, "desc": "Reaction wheel desaturation via magnetorquers complete"}

        elif cmd_name == "GYRO_RECALIBRATE":
            # Recalibrate gyro bias — residual error remains
            old_bias = state.gyro_bias_dps
            state.gyro_bias_dps = random.gauss(0, 0.015)  # small residual, not perfectly zero
            state.attitude_error_deg = max(0.06, state.attitude_error_deg - 2.0)
            return {"applied": True, "desc": f"Gyro bias {old_bias:.3f}→{state.gyro_bias_dps:.4f} dps after recalibration"}

        elif cmd_name == "GPS_RESET":
            # Cold-start GPS reset: gradual re-acquisition (not instant fix)
            state.gps_recovery_ticks = 16   # ~32 seconds cold-start TTFF
            state.gps_satellites = 0        # starts searching from 0
            return {"applied": True, "desc": "GPS cold-start initiated; satellite acquisition in ~30s"}

        elif cmd_name == "HEATER_RELAY_CYCLE":
            # Force-cycle stuck relay — immediate thermal reduction
            relay_id = params.get("relay_id", "BAY_1")
            state.battery_temp_c = max(18.0, state.battery_temp_c - 13.0)
            state.payload_temp_c = max(20.0, state.payload_temp_c - 15.0)
            state.obc_temp_c = max(23.0, state.obc_temp_c - 7.0)
            return {"applied": True, "desc": f"Relay {relay_id} cycled; thermal reduction applied"}

        elif cmd_name == "RADIATOR_SLEW_BIAS":
            # Slew to cold-space radiator view — additional thermal relief
            bias = params.get("bias_angle", 15.0)
            state.battery_temp_c = max(17.0, state.battery_temp_c - 9.0)
            state.payload_temp_c = max(19.0, state.payload_temp_c - 11.0)
            state.attitude_error_deg = max(0.1, state.attitude_error_deg + 0.3)  # slight pointing cost
            return {"applied": True, "desc": f"Radiator slew bias {bias}° applied; heat rejection improved"}

        elif cmd_name == "PAYLOAD_BUFFER_FLUSH":
            # Flush payload buffer and free memory
            backup = params.get("backup_flash", True)
            state.memory_used_pct = max(22.0, state.memory_used_pct - 40.0)
            state.cpu_load_pct = max(18.0, state.cpu_load_pct - 30.0)
            return {"applied": True, "desc": f"Payload buffer flushed{'(backed up)' if backup else ''}; memory {state.memory_used_pct:.1f}%"}

        elif cmd_name == "OBC_SOFT_RESTART_TASK":
            # Restart OBC task: frees some resources
            task = params.get("task_name", "payload_handler")
            state.cpu_load_pct = max(18.0, state.cpu_load_pct - 16.0)
            state.memory_used_pct = max(28.0, state.memory_used_pct - 11.0)
            return {"applied": True, "desc": f"Task '{task}' restarted; partial resource recovery"}

        elif cmd_name == "ANTENNA_GIMBAL_REHOME":
            # Rehome gimbal: significant but not total attenuation recovery
            old_atten = state.rf_attenuation_db
            state.rf_attenuation_db = max(0.0, state.rf_attenuation_db - 17.0)
            state.rf_intermittent = False
            return {"applied": True, "desc": f"Gimbal rehomed; attenuation {old_atten:.1f}→{state.rf_attenuation_db:.1f} dB"}

        elif cmd_name == "COMMS_UHF_FAILOVER":
            # Switch to UHF backup: lower but stable SNR
            state.rf_attenuation_db = max(0.0, state.rf_attenuation_db - 11.0)
            state.rf_intermittent = False
            return {"applied": True, "desc": "UHF backup link active; data rate reduced but stable"}

        elif cmd_name == "LOAD_SHED_NON_ESSENTIAL":
            # Shed non-essential loads
            shed_w = 20.0  # standard load shed
            state.bus_load_w = max(20.0, state.bus_load_w - shed_w)
            return {"applied": True, "desc": f"Load shed {shed_w:.0f}W; bus load {state.bus_load_w:.1f}W"}

        elif cmd_name == "ATTITUDE_HOLD_SUN":
            # Sun-pointing attitude: reduces attitude error + slight solar boost
            bias = params.get("bias_deg", 0.0)
            state.attitude_error_deg = max(0.05, state.attitude_error_deg - 1.2)
            state.solar_efficiency = min(0.98, state.solar_efficiency + 0.07)
            return {"applied": True, "desc": f"Attitude held sun-pointing (bias {bias}°); generation optimized"}

        elif cmd_name == "SAFE_MODE_ENTER":
            # Minimal-power safe mode
            state.bus_load_w = max(16.0, state.bus_load_w - 28.0)
            state.cpu_load_pct = max(12.0, state.cpu_load_pct - 22.0)
            state.attitude_error_deg = max(0.05, state.attitude_error_deg - 2.0)
            return {"applied": True, "desc": "Safe mode entered; minimal-power configuration active"}

        elif cmd_name == "BUS_OVERCURRENT_ISOLATE":
            # Isolate overcurrent source: remove extra load
            extra_w = state.fault_metadata.get("extra_load_w", 38.0)
            state.bus_load_w = max(BUS_NOMINAL_LOAD_W - 5.0, state.bus_load_w - extra_w)
            return {"applied": True, "desc": f"Overcurrent subsystem isolated; {extra_w:.0f}W load removed"}

        elif cmd_name == "RF_POWER_BOOST":
            # Boost RF transmit power
            state.rf_attenuation_db = max(0.0, state.rf_attenuation_db - 5.0)
            return {"applied": True, "desc": "RF transmit power boosted; link margin improved"}

        else:
            return {"applied": False, "desc": f"Command '{cmd_name}' not in recovery engine"}


# ─────────────────────────────────────────────────────────────────────────────
# Spacecraft Physics Engine — State → Correlated Telemetry
# ─────────────────────────────────────────────────────────────────────────────

class SpacecraftPhysics:
    """
    Derives all telemetry values from SpacecraftState.
    This is the ONLY place where spacecraft state becomes telemetry numbers.
    All subsystem relationships are encoded here.
    """

    def step_state(self, state: SpacecraftState, dt_s: float) -> None:
        """
        Update continuous state quantities that evolve each tick independently
        of fault injection. Called every tick before compute_telemetry().
        """
        # ── GPS cold-start recovery ──
        if state.gps_recovery_ticks > 0:
            state.gps_recovery_ticks -= 1
            state.gps_satellites = min(8, state.gps_satellites + 1)
            if state.gps_recovery_ticks == 0 and state.gps_satellites >= 4:
                state.gps_fix = True

        # ── Natural orbital thermal cycling ──
        if state.active_fault not in (
            "battery_overtemperature", "thermal_excursion",
            "solar_thermal_excursion",
        ):
            phase = 2.0 * math.pi * state.orbit_time_s / ORBITAL_PERIOD_S
            state.obc_temp_c += (25.0 + ORBITAL_HEAT_AMPLITUDE * 0.40 * math.sin(phase) - state.obc_temp_c) * 0.018
            state.payload_temp_c += (22.0 + ORBITAL_HEAT_AMPLITUDE * 0.70 * math.sin(phase + 0.5) - state.payload_temp_c) * 0.012
            state.radio_temp_c += (30.0 + ORBITAL_HEAT_AMPLITUDE * 0.30 * math.sin(phase + 0.3) - state.radio_temp_c) * 0.018
            state.battery_temp_c += (20.0 + ORBITAL_HEAT_AMPLITUDE * 0.35 * math.sin(phase) - state.battery_temp_c) * 0.016

        # ── Battery SOC update via power balance ──
        solar_current = self._solar_current(state)
        solar_power_w = solar_current * SOLAR_NOMINAL_VOLTAGE_V * MPPT_EFFICIENCY
        ocv = battery_ocv(state.battery_soc, state.battery_health)
        bus_v_approx = max(20.0, ocv - 0.5)  # rough estimate to avoid circular dependency
        net_power_w = state.bus_load_w - solar_power_w
        batt_current_a = net_power_w / max(20.0, bus_v_approx)  # positive = discharging
        # SOC change per tick (accounting for health / capacity fade)
        capacity_factor = 1.0 + (1.0 - max(0.05, state.battery_health)) * 0.6
        dsoc = -(batt_current_a / BATTERY_CAPACITY_AH) * (dt_s / 3600.0) * 100.0 * capacity_factor
        state.battery_soc = max(0.0, min(100.0, state.battery_soc + dsoc))

        # ── Natural recovery of state when no fault active ──
        if state.active_fault is None:
            # Bus load drifts back toward nominal
            state.bus_load_w += (BUS_NOMINAL_LOAD_W - state.bus_load_w) * 0.04
            # RW speed damps toward nominal
            state.rw_speed_rpm += (RW_NOMINAL_SPEED_RPM - state.rw_speed_rpm) * 0.035
            # Attitude error decays toward nominal
            state.attitude_error_deg = max(0.06, state.attitude_error_deg * 0.95)
            # Gyro bias decays
            state.gyro_bias_dps *= 0.92
            # RF attenuation fades
            state.rf_attenuation_db = max(0.0, state.rf_attenuation_db * 0.88)
            # Battery internal resistance and health recover toward nominal
            state.battery_internal_resistance += (BATTERY_NOMINAL_R_OHM - state.battery_internal_resistance) * 0.08
            state.battery_health = min(1.0, state.battery_health + 0.05)
            state.solar_efficiency = min(1.0, state.solar_efficiency + 0.04)
            state.solar_string_health = min(1.0, state.solar_string_health + 0.04)
            state.battery_temp_c += (20.0 - state.battery_temp_c) * 0.06
            state.payload_temp_c += (22.0 - state.payload_temp_c) * 0.06
            state.obc_temp_c += (25.0 - state.obc_temp_c) * 0.06
            # External disturbance resets
            state.external_disturbance_torque_nm *= 0.85
            # Memory naturally drains
            state.memory_used_pct = max(40.0, state.memory_used_pct - 0.5)
            state.cpu_load_pct += (30.0 - state.cpu_load_pct) * 0.05

    def compute_telemetry(self, state: SpacecraftState) -> Dict[str, float]:
        """
        Derive all telemetry values from SpacecraftState via physics relationships.
        Returns the complete telemetry dict — original 12 keys + 9 new physics keys.
        All values are physically correlated through the state model.
        """
        # ── EPS: Solar Generation ──
        solar_current_a = self._solar_current(state)
        solar_power_w = solar_current_a * SOLAR_NOMINAL_VOLTAGE_V * MPPT_EFFICIENCY

        # ── EPS: Battery ──
        ocv = battery_ocv(state.battery_soc, state.battery_health)
        r_eff = effective_internal_resistance(
            state.battery_internal_resistance,
            state.battery_temp_c,
            state.battery_health
        )
        bus_v_approx = max(20.0, ocv - 0.5)
        net_power_w = state.bus_load_w - solar_power_w
        # If battery is degraded (health < 0.75), charging acceptance is limited (BMS protection)
        if net_power_w < 0:
            charge_factor = min(1.0, max(0.0, (state.battery_health - 0.5) / 0.5))
            battery_current_a = (net_power_w * charge_factor) / max(20.0, bus_v_approx)
        else:
            battery_current_a = net_power_w / max(20.0, bus_v_approx)
        # Terminal voltage: V_t = OCV - I*R
        battery_voltage_v = ocv - battery_current_a * r_eff
        battery_voltage_v = max(18.0, min(34.5, battery_voltage_v))

        # ── EPS: Bus ──
        bus_voltage_v = battery_voltage_v * 0.975  # small diode + trace drop
        bus_power_w = state.bus_load_w

        # ── ADCS: Reaction Wheel ──
        # Current proportional to speed magnitude and correction torque
        rw_speed_norm = abs(state.rw_speed_rpm) / max(1.0, RW_MAX_SPEED_RPM)
        rw_current_a = 0.18 + rw_speed_norm * 3.0 + abs(state.external_disturbance_torque_nm) * 150.0

        # ── ADCS: Attitude & Gyro ──
        gyro_contribution_to_error = abs(state.gyro_bias_dps) * 1.9
        attitude_error_eff = state.attitude_error_deg + gyro_contribution_to_error

        # Gyro sensor readings: true rate + bias + noise
        gyro_x = state.gyro_bias_dps * 0.55 + random.gauss(0, GYRO_NOISE_DPS)
        gyro_y = state.gyro_bias_dps * 0.30 + random.gauss(0, GYRO_NOISE_DPS)
        gyro_z = state.gyro_bias_dps * 0.15 + random.gauss(0, GYRO_NOISE_DPS)

        # ── COMMS ──
        rssi_dbm = NOMINAL_RSSI_DBM - state.rf_attenuation_db + random.gauss(0, 1.3)
        snr_db = max(0.0, NOMINAL_SNR_DB - state.rf_attenuation_db * 0.92 + random.gauss(0, 0.4))
        # Packet loss: near-zero at high SNR, rises rapidly below 15 dB
        if snr_db > 20.0:
            packet_loss_pct = max(0.0, random.gauss(0.4, 0.3))
        elif snr_db > 12.0:
            packet_loss_pct = max(0.0, (20.0 - snr_db) * 1.8 + random.gauss(0, 1.2))
        else:
            packet_loss_pct = max(0.0, 15.0 + (12.0 - snr_db) * 5.5 + random.gauss(0, 3.0))
        packet_loss_pct = min(100.0, packet_loss_pct)

        # ── GPS ──
        gps_fix_val = 1.0 if state.gps_fix else 0.0
        gps_sats = max(0, state.gps_satellites + (random.randint(-1, 1) if state.gps_satellites > 0 else 0))
        gps_sats = min(16, gps_sats)

        # ── OBC ──
        cpu = state.cpu_load_pct
        mem = state.memory_used_pct

        def n(sigma: float) -> float:
            return random.gauss(0, sigma)

        # ─────────────────────────────────────────────────────────────────────
        # Return correlated telemetry dict
        # Original 12 keys preserved with identical names for backward compat.
        # 9 new physics keys appended.
        # ─────────────────────────────────────────────────────────────────────
        return {
            # ── Original 12 (preserved keys) ──
            "battery_voltage_v":  round(max(18.0, battery_voltage_v + n(0.04)), 3),
            "battery_soc_pct":    round(max(0.0, min(100.0, state.battery_soc + n(0.25))), 3),
            "solar_current_a":    round(max(0.0, solar_current_a + n(0.07)), 3),
            "bus_power_w":        round(max(10.0, bus_power_w + n(0.6)), 3),
            "temp_obc_c":         round(state.obc_temp_c + n(0.18), 3),
            "temp_battery_c":     round(state.battery_temp_c + n(0.10), 3),
            "temp_payload_c":     round(state.payload_temp_c + n(0.22), 3),
            "attitude_error_deg": round(max(0.0, attitude_error_eff + n(0.018)), 3),
            "reaction_wheel_rpm": round(state.rw_speed_rpm + n(14.0), 3),
            "downlink_snr_db":    round(snr_db, 3),
            "memory_usage_pct":   round(max(0.0, min(100.0, mem + n(0.18))), 3),
            "cpu_usage_pct":      round(max(0.0, min(100.0, cpu + n(1.1))), 3),
            # ── 9 New physics params ──
            "bus_voltage_v":      round(max(18.0, bus_voltage_v + n(0.035)), 3),
            "battery_current_a":  round(battery_current_a + n(0.035), 3),
            "solar_power_w":      round(max(0.0, solar_power_w + n(0.25)), 3),
            "gyro_bias_dps":      round(state.gyro_bias_dps + n(GYRO_NOISE_DPS), 4),
            "rw_current_a":       round(max(0.0, rw_current_a + n(0.04)), 3),
            "rssi_dbm":           round(rssi_dbm, 2),
            "packet_loss_pct":    round(packet_loss_pct, 2),
            "gps_fix":            float(gps_fix_val),
            "gps_satellites":     float(gps_sats),
        }

    def _solar_current(self, state: SpacecraftState) -> float:
        """Compute solar array current from state."""
        # Panel temperature coefficient (slight output reduction at high temps)
        panel_temp_c = state.battery_temp_c + 18.0  # rough panel temp estimate
        temp_coeff = 1.0 - max(0.0, (panel_temp_c - 25.0)) * 0.003
        return max(0.0,
            SOLAR_NOMINAL_CURRENT_A
            * state.solar_efficiency
            * state.solar_string_health
            * state.sun_exposure
            * temp_coeff
        )
