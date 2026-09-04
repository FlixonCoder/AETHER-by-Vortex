"""
Identifier Agent for Satellite Mission Operations.
Performs root-cause analysis, synthesizes multiple hypotheses with probabilities and evidence,
and leverages historical RAG incident records.

INVARIANT:
The Identifier Agent is purely diagnostic and cannot execute commands.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from .llm_provider import LLMProvider, safe_number
from .rag_memory import RAGMemory


class IdentifierAgent:
    """Diagnoses root cause, compares historical incidents, and produces structured hypotheses."""

    def __init__(self, llm_provider: LLMProvider, rag_memory: RAGMemory):
        self.llm = llm_provider
        self.rag = rag_memory

    async def identify(
        self,
        anomaly: dict,
        current_telemetry: Dict[str, float],
        history: Dict[str, List[dict]],
        orbital_ctx: Optional[dict] = None
    ) -> dict:
        incident_id = anomaly.get("incident_id") or anomaly.get("id")
        subsys = anomaly.get("primary_subsystem", "OBC")
        ano_type = anomaly.get("anomaly_type", "")

        # 1. Query RAG for deep incident similarity
        query_text = f"{subsys} {ano_type} {anomaly.get('summary', '')}"
        rag_incidents = self.rag.retrieve_similar_incidents(query_text, k=3)
        rag_context_ids = [inc.get("incident_id") for inc in rag_incidents if inc.get("incident_id")]

        # 2. Rule fallback
        def _rule_fallback():
            return self.llm.rule_engine.identify(anomaly, history, rag_incidents)

        # 3. LLM Diagnostic Reasoning Prompt
        rag_summary = "\n".join([
            f"- Historical {inc.get('incident_id')}: {inc.get('root_cause')} (Outcome: {inc.get('outcome')})"
            for inc in rag_incidents
        ]) or "None on record."

        prompt = f"""You are a satellite systems diagnostic engineer performing root-cause analysis.
ANOMALY REPORT:
{json.dumps(anomaly, indent=2)}

CURRENT TELEMETRY:
{json.dumps(current_telemetry, indent=2)}

HISTORICAL INCIDENTS FROM RAG MEMORY:
{rag_summary}

Analyze the telemetry patterns, evaluate probable causes, and formulate multiple ranked hypotheses.
Respond ONLY with a valid JSON object (no markdown) with these keys:
- root_cause: clear explanation of the primary mechanical/electrical/software cause
- confidence: float between 0.0 and 1.0
- hypotheses: list of objects, each with:
    - cause: concise description
    - probability: float 0.0-1.0 (probabilities should sum close to 1.0)
    - evidence: list of string observations supporting this hypothesis
- reasoning: brief step-by-step diagnostic reasoning chain
"""
        system = "You are an expert satellite mission diagnostician. Output only raw valid JSON without markdown wrapping."

        result, mode_used = await self.llm.generate_json(
            prompt=prompt,
            system_instruction=system,
            agent_role="IDENTIFIER",
            fallback_handler=_rule_fallback,
            timeout=16.0
        )

        # Ensure hypotheses structure
        hypotheses = result.get("hypotheses", [])
        if not hypotheses or not isinstance(hypotheses, list):
            fallback_res = _rule_fallback()
            hypotheses = fallback_res.get("hypotheses", [])

        return {
            "incident_id": incident_id,
            "anomaly_id": incident_id,
            "subsystem": subsys,
            "root_cause": result.get("root_cause", "Subsystem anomaly requiring procedure execution."),
            "confidence": safe_number(result.get("confidence"), default=0.88, lo=0.0, hi=1.0),
            "trust_score": round(safe_number(result.get("confidence"), default=0.88, lo=0.0, hi=1.0) * 100),
            "hypotheses": hypotheses,
            "reasoning": result.get("reasoning", "Diagnostic signature isolated via multi-agent analysis."),
            "rag_context_ids": rag_context_ids,
            "rag_matches": rag_incidents,
            "llm_mode": mode_used,
            "diagnosed_at": datetime.now(timezone.utc).isoformat()
        }
