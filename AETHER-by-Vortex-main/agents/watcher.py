"""
Watcher Agent for Satellite Mission Operations.
Monitors telemetry streams, detects boundary violations and anomalous trends,
retrieves matching historical incidents from RAG memory, and applies
the deterministic Criticality Engine.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import TELEMETRY_PARAMS
from model.live_adapter import classify_from_live_telemetry
from telemetry.simulator import TelemetrySnapshot
from .criticality_engine import CriticalityEngine
from .llm_provider import LLMProvider, safe_number
from .rag_memory import RAGMemory

# The only subsystems that actually exist in TELEMETRY_PARAMS. An LLM tier can
# name anything in free text (e.g. "SOLAR_PANEL_ARRAY", "GPS"); if it's not one
# of these, the classification is a hallucination and must not steer routing,
# the criticality engine's subsystem weighting, or the operator-facing report.
KNOWN_SUBSYSTEMS = {"EPS", "THERMAL", "COMMS", "ADCS", "OBC"}


class WatcherAgent:
    """Watches telemetry, queries RAG, and produces verified anomaly incidents."""

    def __init__(self, llm_provider: LLMProvider, rag_memory: RAGMemory, criticality_engine: CriticalityEngine):
        self.llm = llm_provider
        self.rag = rag_memory
        self.criticality_engine = criticality_engine
        self.consecutive_nominal = 0

    async def analyze(
        self,
        snapshot: TelemetrySnapshot,
        history: Dict[str, List[dict]],
        orbital_ctx: Optional[dict] = None,
        anomaly_type_hint: Optional[str] = None
    ) -> Optional[dict]:
        violations = snapshot.violations()
        if not violations:
            self.consecutive_nominal += 1
            return None

        self.consecutive_nominal = 0
        incident_id = f"ANO-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')[:12]}"

        # 1. First extract violated parameters and query RAG memory
        violation_summary = " ".join([f"{v['subsystem']} {v['param']} {v['direction']}" for v in violations])
        rag_matches = self.rag.retrieve_similar_incidents(violation_summary, k=3)

        # 2. Deterministic Fallback Classifier
        def _rule_fallback():
            ml_result = classify_from_live_telemetry(snapshot.values, history, violations)
            if ml_result is not None:
                return ml_result
            return self.llm.rule_engine.watcher_classify(violations, snapshot.values)

        # 3. Prompt for LLM classification (Ollama -> Groq -> Rule Engine)
        current_str = json.dumps({p: {"val": snapshot.values[p], "unit": snapshot.units.get(p, "")} for p in snapshot.values})
        viols_str = json.dumps(violations)
        hist_summary = {v["param"]: [round(r.get("value", 0.0), 2) for r in history.get(v["param"], [])[-5:]] for v in violations}

        prompt = f"""You are a satellite Watcher agent monitoring spacecraft telemetry.
CURRENT TELEMETRY: {current_str}
THRESHOLD VIOLATIONS: {viols_str}
RECENT PARAMETER TRENDS: {json.dumps(hist_summary)}
ORBITAL CONTEXT: eclipse={snapshot.in_eclipse}, phase={round(snapshot.orbital_phase * 100, 1)}%

Respond ONLY with a valid JSON object (no markdown) with these exact keys:
- anomaly_type: concise string label (e.g. "battery_undervoltage", "thermal_excursion", "attitude_drift", "memory_overflow", "comms_loss")
- primary_subsystem: EPS, ADCS, COMMS, THERMAL, or OBC
- summary: one sentence describing the failure
- affected_params: list of string parameter names
- trend: "worsening" | "stable" | "improving"
- confidence: float between 0.0 and 1.0
- reasoning: one or two sentences on WHY you classified it this way -- which violated parameters and trends drove the call
"""
        system = "You are an autonomous satellite telemetry monitor. You output only raw, valid JSON."
        classification, mode_used = await self.llm.generate_json(
            prompt=prompt,
            system_instruction=system,
            agent_role="WATCHER",
            fallback_handler=_rule_fallback,
            timeout=12.0
        )

        subsys = str(classification.get("primary_subsystem", "")).upper()
        if subsys not in KNOWN_SUBSYSTEMS:
            # Ground it in the actual violated telemetry instead of trusting a
            # free-text label the LLM tier invented.
            subsys = str(violations[0].get("subsystem", "OBC")).upper()
            if subsys not in KNOWN_SUBSYSTEMS:
                subsys = "OBC"

        ano_type = anomaly_type_hint or classification.get("anomaly_type")

        # 4. DETERMINISTIC CRITICALITY ENGINE EVALUATION
        # Invariant: Criticality is calculated mathematically, NEVER dictated by LLM
        crit_eval = self.criticality_engine.evaluate(
            subsystem=subsys,
            violations=violations,
            current_telemetry=snapshot.values,
            history=history,
            orbital_ctx=orbital_ctx,
            rag_similar_incidents=rag_matches,
            anomaly_type=ano_type
        )

        evidence = [
            f"{v['param']} = {v['value']} exceeds {v['direction']} limit ({v['threshold']})"
            for v in violations
        ]

        return {
            "incident_id": incident_id,
            "id": incident_id,  # compatibility alias
            "anomaly_detected": True,
            "anomaly_type": classification.get("anomaly_type", "threshold_violation"),
            "primary_subsystem": subsys,
            "affected_subsystem": subsys,
            "summary": classification.get("summary", f"Telemetry anomaly detected in {subsys}"),
            "affected_params": classification.get("affected_params", [v["param"] for v in violations]),
            "trend": classification.get("trend", "stable"),
            "confidence": safe_number(classification.get("confidence"), default=0.85, lo=0.0, hi=1.0),
            "reasoning": classification.get("reasoning", f"{len(violations)} threshold violation(s) matched to {subsys} against recent trend data."),
            "trust_score": round(safe_number(classification.get("confidence"), default=0.85, lo=0.0, hi=1.0) * 100),
            # Strictly use deterministic criticality outputs:
            "severity": crit_eval["severity"],
            "criticality_score": crit_eval["criticality_score"],
            "criticality_policy": crit_eval["policy"],
            "criticality_factors": crit_eval["factors"],
            "evidence": evidence,
            "telemetry_snapshot": snapshot.values,
            "violations": violations,
            "orbital": orbital_ctx or {},
            "rag_matches": rag_matches,
            "llm_mode": mode_used,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "tick": snapshot.tick
        }
