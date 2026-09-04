"""
Runbook Generator Agent
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from config import REASONING_MODEL
from .llm import acreate, get_client

class RunbookGenerator:
    def __init__(self, runbook_dir: str = "runbooks"):
        self.self_client = get_client()
        self.self_runbook_dir = Path(runbook_dir)
        self.self_runbook_dir.mkdir(exist_ok=True)

    async def generate(self, anomaly: dict, diagnosis: dict, validated_plan: dict, approved_rank: int) -> dict:
        approved_option = next((o for o in validated_plan["validated_options"] if o["rank"] == approved_rank), validated_plan["validated_options"][0])

        prompt = f"""Write an operator runbook in markdown format. Include sections: Title, Situation Summary, Root Cause, Checklist, Execution steps with [CMD]/[MON]/[VER] markers.
ANOMALY: {json.dumps(anomaly)}
DIAGNOSIS: {json.dumps(diagnosis)}
OPTION: {json.dumps(approved_option)}
"""
        try:
            resp = await acreate(
                self.self_client,
                model=REASONING_MODEL,
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            runbook_md = resp.content[0].text.strip()
        except Exception as e:
            print(f"[RUNBOOK_GENERATOR] LLM call failed, using fallback template: {e}", flush=True)
            runbook_md = (
                f"# RUNBOOK: {anomaly.get('summary', 'Anomaly Response')}\n\n"
                f"**ID:** {anomaly['id']}  **Severity:** {anomaly.get('severity', '-')}  "
                f"**Subsystem:** {anomaly.get('primary_subsystem', '-')}\n\n"
                f"## Root Cause\n{diagnosis.get('root_cause', 'See diagnostics report.')}\n\n"
                f"## Approved Procedure: {approved_option.get('name', '-')}\n"
                + "\n".join(f"- [CMD] {s}" for s in approved_option.get("steps", []))
            )

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"runbook_{anomaly['id']}_{ts}.md"
        (self.self_runbook_dir / filename).write_text(runbook_md, encoding="utf-8")

        return {"anomaly_id": anomaly["id"], "filename": filename, "content": runbook_md, "generated_at": datetime.now(timezone.utc).isoformat(), "approved_procedure": approved_option["name"]}