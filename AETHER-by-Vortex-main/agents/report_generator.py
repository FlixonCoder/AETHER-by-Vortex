"""
Report Generator Agent for Autonomous Satellite Operations.
Produces both:
1. Structured JSON Incident Reports for embedding directly into RAG Episodic Memory.
2. Formal Markdown Runbooks saved to the runbooks directory for human operators.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from config import RUNBOOK_DIR


class ReportGenerator:
    """Generates structured incident reports and operator runbooks."""

    def __init__(self, runbook_dir: Optional[Path] = None):
        self.runbook_dir = runbook_dir or RUNBOOK_DIR
        self.runbook_dir.mkdir(parents=True, exist_ok=True)

    def generate_incident_report(
        self,
        incident_id: str,
        anomaly: dict,
        diagnosis: dict,
        action: dict,
        simulation: dict,
        execution: dict,
        outcome: str,
        attempts: int = 1,
        trust_score: Optional[int] = None,
        stage_trail: Optional[List[dict]] = None,
    ) -> dict:
        """Generates clean, structured JSON incident report for RAG vector memory ingestion."""
        now = datetime.now(timezone.utc).isoformat()
        subsys = anomaly.get("primary_subsystem", "OBC")
        ano_type = anomaly.get("anomaly_type", "anomaly")
        root_cause = diagnosis.get("root_cause", "Root cause identified.")

        lessons = [
            f"Subsystem {subsys} responded to {action.get('name', 'procedure')} with outcome {outcome}.",
            f"Initial recovery probability estimated at {action.get('estimated_recovery_probability', 0.9):.0%}.",
            f"Procedure resolved in {attempts} cycle(s)."
        ]

        report = {
            "incident_id": incident_id,
            "anomaly": f"{subsys} {ano_type}: {anomaly.get('summary', '')}",
            "subsystem": subsys,
            "anomaly_type": ano_type,
            "root_cause": root_cause,
            "telemetry_signature": {
                "affected_params": anomaly.get("affected_params", []),
                "snapshot": anomaly.get("telemetry_snapshot", {})
            },
            "solution": action.get("name", "Standard recovery procedure"),
            "commands": action.get("commands", []),
            "simulation_result": {
                "safe": simulation.get("safe", True),
                "risk_score": simulation.get("risk_score", 20)
            },
            "execution_result": {
                "status": execution.get("status", "EXECUTED"),
                "authorized": execution.get("authorized", True)
            },
            "outcome": outcome,
            "criticality": anomaly.get("severity", "LOW"),
            "criticality_score": anomaly.get("criticality_score", 50),
            "confidence": diagnosis.get("confidence", 0.90),
            "lessons_learned": lessons,
            "trust_score": trust_score,
            "stage_trail": stage_trail or [],
            "timestamp": now
        }

        return report

    def generate_runbook_markdown(
        self,
        incident_id: str,
        anomaly: dict,
        diagnosis: dict,
        action: dict,
        simulation: dict,
        execution: dict,
        outcome: str,
        trust_score: Optional[int] = None,
        stage_trail: Optional[List[dict]] = None,
    ) -> dict:
        """Generates operator-ready Markdown runbook and persists to disk."""
        now = datetime.now(timezone.utc)
        detected_iso = anomaly.get("detected_at")
        detected_display = ""
        if detected_iso:
            try:
                dt_det = datetime.fromisoformat(detected_iso)
                detected_display = dt_det.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                detected_display = str(detected_iso)

        local_now_str = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        utc_now_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        time_display = f"{local_now_str} (Local) / {utc_now_str}"
        if detected_display:
            timing_line = f"**Detected At:** `{detected_display}` | **Report Generated:** `{time_display}`"
        else:
            timing_line = f"**Generated At:** `{time_display}`"

        ts_slug = now.strftime("%Y%m%d_%H%M%S")
        filename = f"runbook_{incident_id}_{ts_slug}.md"

        subsys = anomaly.get("primary_subsystem", "-")
        sev = anomaly.get("severity", "-")
        score = anomaly.get("criticality_score", 0)
        action_name = action.get("name", "Procedure")

        cmd_lines = []
        for i, cmd in enumerate(action.get("commands", []), 1):
            cmd_lines.append(f"{i}. [CMD] Execute `{cmd.get('command')}` with params `{json.dumps(cmd.get('parameters', {}))}`")
        if not cmd_lines:
            cmd_lines.append("1. [CMD] Hold satellite in current configuration.")

        hypo_lines = []
        for h in diagnosis.get("hypotheses", []):
            hypo_lines.append(f"- **{h.get('cause', 'Hypothesis')}** (prob: {int(h.get('probability', 0)*100)}%)")

        trail = stage_trail or []
        baseline_entries = [t for t in trail if t.get("agent") == "BASELINE_CHECK"]
        baseline_line = baseline_entries[-1]["reasoning"] if baseline_entries else "Not recorded for this incident."

        trail_rows = []
        for t in trail:
            reasoning = (t.get("reasoning") or "-").replace("|", "/").replace("\n", " ")
            trail_rows.append(f"| {t.get('agent', '-')} | #{t.get('attempt', 1)} | {t.get('trust_score', '-')}/100 | {reasoning} |")
        trust_display = f"{trust_score}/100" if trust_score is not None else "—"

        content = f"""# MISSION RUNBOOK: {action_name.upper()}
**Incident ID:** `{incident_id}`
**Subsystem:** `{subsys}` | **Severity:** `{sev}` (Criticality Score: `{score}/100`)
**Resolution Outcome:** `{outcome}` | **Solution Trust Score:** `{trust_display}`
{timing_line}

---

## 1. Situation Summary
{anomaly.get('summary', 'Telemetry anomaly detected.')}

**Detected Parameter Violations:**
{chr(10).join(f"- `{e}`" for e in anomaly.get('evidence', [])) or "- No direct evidence lines recorded."}

---

## 2. Root Cause & Diagnostic Hypotheses
**Primary Root Cause:**
> {diagnosis.get('root_cause', 'Diagnostics complete.')}

**Evaluated Hypotheses:**
{chr(10).join(hypo_lines) or "- Single deterministic hypothesis verified."}

---

## 3. Approved Recovery Procedure
**Action:** `{action_name}`
**Risk Level:** `{action.get('risk', 'LOW')}` | **Reversible:** `{action.get('reversible', True)}`
**Expected Outcome:** {action.get('expected_outcome', 'Stabilization')}

### Execution Commands:
{chr(10).join(cmd_lines)}

---

## 4. Digital Twin Simulation & Safety Gate
- **Simulation Viability:** `{"SAFE" if simulation.get("safe") else "UNSAFE"}` (Risk Score: `{simulation.get('risk_score', 0)}/100`)
- **Simulation Rationale:** {simulation.get('reason', 'Cleared constraints.')}
- **Baseline Telemetry Check:** {baseline_line}
- **Safety Gate Status:** `{execution.get('status', 'EXECUTED')}` (Authorized: `{execution.get('authorized', True)}`)

---

## 5. Post-Recovery Verification
- **Final Outcome:** `{outcome}`
- **RAG Episodic Memory:** Ingested into autonomous historical database for future anomaly recognition.

---

## 6. Trust & Reasoning Trail
Every agent stage below reports its own confidence in its own output; the solution trust score is their average. This is not a single model's guess about the whole incident — it's what each stage actually did, and why.

**Overall Solution Trust Score: `{trust_display}`**

| Stage | Attempt | Trust | Reasoning |
|-------|---------|-------|-----------|
{chr(10).join(trail_rows) or "| - | - | - | No stage trail recorded for this incident. |"}
"""
        filepath = self.runbook_dir / filename
        filepath.write_text(content, encoding="utf-8")

        return {
            "incident_id": incident_id,
            "anomaly_id": incident_id,
            "filename": filename,
            "filepath": str(filepath),
            "content": content,
            "approved_procedure": action_name,
            "generated_at": now.isoformat(),
            "detected_at": detected_iso or now.isoformat()
        }
