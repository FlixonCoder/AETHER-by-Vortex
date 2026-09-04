"""
Fix Finder Agent for Satellite Mission Operations.
Generates ranked candidate recovery actions adhering to the spacecraft Command Whitelist.
Leverages RAG procedural memory to select previously proven procedures.
"""
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import COMMAND_WHITELIST
from .llm_provider import LLMProvider, safe_number
from .rag_memory import RAGMemory


class FixFinderAgent:
    """Retrieves proven operational procedures and synthesizes candidate recovery actions."""

    def __init__(self, llm_provider: LLMProvider, rag_memory: RAGMemory):
        self.llm = llm_provider
        self.rag = rag_memory

    async def find_fixes(
        self,
        anomaly: dict,
        diagnosis: dict,
        current_telemetry: Dict[str, float],
        orbital_ctx: Optional[dict] = None
    ) -> dict:
        incident_id = anomaly.get("incident_id") or anomaly.get("id")
        subsys = anomaly.get("primary_subsystem", "OBC")
        ano_type = anomaly.get("anomaly_type", "")

        # 1. Retrieve procedural knowledge from RAG memory
        rag_procs = self.rag.retrieve_procedures(subsystem=subsys, query_text=ano_type, k=3)

        # 2. Rule fallback
        def _rule_fallback():
            candidates = self.llm.rule_engine.find_fixes(anomaly, diagnosis, rag_procs)
            return {"candidates": candidates, "recommended_action_id": candidates[0]["action_id"] if candidates else "ACT-01"}

        # 3. Formulate LLM Prompt
        whitelist_summary = {k: v["description"] for k, v in COMMAND_WHITELIST.items()}
        rag_summary = json.dumps([{
            "id": p.get("procedure_id"),
            "name": p.get("name"),
            "success_rate": p.get("success_rate"),
            "commands": p.get("commands")
        } for p in rag_procs], indent=2)

        prompt = f"""You are a satellite mission recovery engineer. Generate candidate recovery actions.
ANOMALY:
{json.dumps(anomaly, indent=2)}

DIAGNOSIS & ROOT CAUSE:
{json.dumps(diagnosis, indent=2)}

CURRENT TELEMETRY:
{json.dumps(current_telemetry, indent=2)}

COMMAND WHITELIST (ONLY USE COMMANDS FROM THIS LIST):
{json.dumps(whitelist_summary, indent=2)}

HISTORICAL PROCEDURAL MEMORY FROM RAG:
{rag_summary}

Generate 2-3 candidate recovery options. Every command MUST be drawn strictly from the COMMAND WHITELIST.
Respond ONLY with a valid JSON object (no markdown) with these keys:
- candidates: list of candidate objects, each with:
    - action_id: string e.g. "ACT-01"
    - name: short title
    - description: procedural summary
    - commands: list of command objects with "command" (exact string from whitelist) and "parameters" dict
    - expected_outcome: expected telemetry change
    - risk: "LOW", "MEDIUM", or "HIGH"
    - estimated_recovery_probability: float 0.0-1.0
    - mission_impact: statement of impact on science/spacecraft operations
    - reversible: boolean
- recommended_action_id: action_id of the top recommendation
- reasoning: one or two sentences on WHY the recommended action was chosen over the alternatives
"""
        system = "You are a satellite recovery planning agent. Output only raw valid JSON. All commands must match the whitelist."

        result, mode_used = await self.llm.generate_json(
            prompt=prompt,
            system_instruction=system,
            agent_role="FIX_FINDER",
            fallback_handler=_rule_fallback,
            timeout=16.0
        )

        candidates = result.get("candidates", [])
        if not candidates or not isinstance(candidates, list):
            fallback_res = _rule_fallback()
            candidates = fallback_res["candidates"]

        # Validate that commands strictly exist in whitelist
        sanitized_candidates = []
        for cand in candidates:
            valid_cmds = []
            for c in cand.get("commands", []):
                cmd_name = c.get("command")
                if cmd_name in COMMAND_WHITELIST:
                    valid_cmds.append({"command": cmd_name, "parameters": c.get("parameters", {})})
            if not valid_cmds:
                # Assign default safe fallback command from whitelist
                valid_cmds = [{"command": "SAFE_MODE_ENTER", "parameters": {}}]

            cand["commands"] = valid_cmds
            sanitized_candidates.append(cand)

        recommended_id = result.get("recommended_action_id") or (sanitized_candidates[0]["action_id"] if sanitized_candidates else "ACT-01")
        recommended = next((c for c in sanitized_candidates if c.get("action_id") == recommended_id), sanitized_candidates[0] if sanitized_candidates else {})
        recovery_prob = safe_number(recommended.get("estimated_recovery_probability"), default=0.85, lo=0.0, hi=1.0)

        return {
            "incident_id": incident_id,
            "anomaly_id": incident_id,
            "candidates": sanitized_candidates,
            "recommended_action_id": recommended_id,
            "rag_procedures_evaluated": [p.get("procedure_id") for p in rag_procs],
            "reasoning": result.get("reasoning", f"'{recommended.get('name', 'candidate')}' selected as the highest-confidence whitelisted procedure for this subsystem."),
            "trust_score": round(recovery_prob * 100),
            "llm_mode": mode_used,
            "generated_at": datetime.now(timezone.utc).isoformat()
        }
