"""
Audit Logger for Autonomous Satellite Mission Operations.
Provides immutable, append-only traceability of all AI agent decisions,
criticality evaluations, simulation passes, approvals, and spacecraft command executions.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config import AUDIT_DIR


class AuditLogger:
    """Maintains an append-only JSONL audit log and an in-memory buffer for the frontend."""

    def __init__(self, audit_dir: Optional[Path] = None):
        self.audit_dir = audit_dir or AUDIT_DIR
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self.audit_file = self.audit_dir / "audit.jsonl"
        self._memory_buffer: List[dict] = []
        self._load_tail()

    def _load_tail(self, limit: int = 150):
        """Loads recent audit entries into in-memory ring buffer."""
        if not self.audit_file.exists():
            return
        lines = []
        try:
            with open(self.audit_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        lines.append(line)
            for l in lines[-limit:]:
                try:
                    self._memory_buffer.append(json.loads(l))
                except json.JSONDecodeError:
                    pass
        except Exception:
            pass

    def log(
        self,
        incident_id: str,
        agent: str,
        action: str,
        input_data: Any = None,
        output_data: Any = None,
        rag_context_ids: Optional[List[str]] = None,
        criticality: Optional[Dict] = None,
        llm_mode: Optional[str] = None,
        simulation_result: Optional[Dict] = None,
        validator_result: Optional[Dict] = None,
        human_approval: Optional[Dict] = None,
        execution_result: Optional[Dict] = None,
        final_outcome: Optional[str] = None
    ) -> dict:
        """Records an auditable operation."""
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "incident_id": incident_id or "GENERAL",
            "agent": agent,
            "action": action,
            "input": input_data,
            "output": output_data,
            "rag_context_ids": rag_context_ids or [],
            "criticality": criticality,
            "llm_mode": llm_mode or "RULE_BASED",
            "simulation_result": simulation_result,
            "validator_result": validator_result,
            "human_approval": human_approval,
            "execution_result": execution_result,
            "final_outcome": final_outcome
        }

        # Append to file
        try:
            with open(self.audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            print(f"[AUDIT_LOGGER] Failed to write log to disk: {e}", flush=True)

        # Append to in-memory buffer
        self._memory_buffer.append(record)
        if len(self._memory_buffer) > 200:
            self._memory_buffer.pop(0)

        return record

    def get_entries(self, limit: int = 50, incident_id: Optional[str] = None) -> List[dict]:
        """Returns recent audit logs, optionally filtered by incident_id."""
        if incident_id:
            filtered = [r for r in self._memory_buffer if r.get("incident_id") == incident_id]
            return filtered[-limit:]
        return self._memory_buffer[-limit:]
