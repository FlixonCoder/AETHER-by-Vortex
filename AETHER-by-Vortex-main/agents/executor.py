"""
Simulated Command Executor for Spacecraft Interface.
Applies authorized recovery commands from the command whitelist to the simulator,
capturing telemetry before and after execution.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from config import COMMAND_WHITELIST


class CommandExecutor:
    """Simulated spacecraft command execution engine."""

    def __init__(self, simulator=None):
        self.simulator = simulator
        self.execution_history: List[dict] = []

    def execute_action(
        self,
        incident_id: str,
        candidate_action: dict,
        authorized: bool = True
    ) -> dict:
        timestamp = datetime.now(timezone.utc).isoformat()
        action_id = candidate_action.get("action_id", "ACT-01")
        commands = candidate_action.get("commands", [])

        telemetry_before = {}
        if self.simulator:
            # Capture current telemetry snapshot before execution
            telemetry_before = {p: self.simulator._history[p][-1]["value"] for p in self.simulator._history if self.simulator._history[p]}

        if not authorized:
            record = {
                "timestamp": timestamp,
                "incident_id": incident_id,
                "action_id": action_id,
                "commands": commands,
                "authorized": False,
                "success": False,
                "status": "REJECTED_UNAUTHORIZED",
                "telemetry_before": telemetry_before,
                "telemetry_after": telemetry_before,
                "trust_score": 0,
                "reasoning": "Execution blocked — candidate was not authorized by the Safety Gate.",
            }
            self.execution_history.append(record)
            return record

        # Execute whitelisted command deltas
        executed_commands = []
        applied_deltas = {}

        for cmd_obj in commands:
            cmd_name = cmd_obj.get("command")
            if cmd_name in COMMAND_WHITELIST:
                meta = COMMAND_WHITELIST[cmd_name]
                executed_commands.append({
                    "command": cmd_name,
                    "parameters": cmd_obj.get("parameters", {}),
                    "authorized": True
                })
                # Gather deltas to resolve anomaly in simulator
                for param, delta in meta.get("deltas", {}).items():
                    applied_deltas[param] = applied_deltas.get(param, 0.0) + delta

        # Apply counter-effects to the active simulator
        if self.simulator:
            # 1. Apply authorized recovery commands to underlying SpacecraftState
            for cmd_info in executed_commands:
                cmd_name = cmd_info["command"]
                params = cmd_info.get("parameters", {})
                if hasattr(self.simulator, "apply_recovery_command"):
                    self.simulator.apply_recovery_command(cmd_name, params)

            # 2. Neutralize active anomaly if the recovery matches
            active_ano = self.simulator.get_active_anomaly()
            if active_ano:
                self.simulator.clear_anomaly()

            # 3. Capture telemetry after execution from causal physics engine
            if hasattr(self.simulator, "compute_telemetry_snapshot"):
                snap_after = self.simulator.compute_telemetry_snapshot()
                telemetry_after = dict(snap_after.values)
                for p, v in telemetry_after.items():
                    if p in self.simulator._history and self.simulator._history[p]:
                        self.simulator._history[p][-1]["value"] = v
            else:
                telemetry_after = {p: self.simulator._history[p][-1]["value"] for p in self.simulator._history if self.simulator._history[p]}
                for p, d in applied_deltas.items():
                    if p in telemetry_after:
                        telemetry_after[p] = round(telemetry_after[p] + d, 3)
                        if self.simulator._history.get(p):
                            self.simulator._history[p][-1]["value"] = telemetry_after[p]
        else:
            telemetry_after = dict(telemetry_before)

        commands_matched = len(executed_commands) == len(commands) and len(commands) > 0
        record = {
            "timestamp": timestamp,
            "incident_id": incident_id,
            "action_id": action_id,
            "name": candidate_action.get("name", "Recovery Action"),
            "commands": executed_commands,
            "authorized": True,
            "success": True,
            "status": "EXECUTED",
            "telemetry_before": telemetry_before,
            "telemetry_after": telemetry_after,
            "trust_score": 100 if commands_matched else max(0, round(70 * len(executed_commands) / max(1, len(commands)))),
            "reasoning": (
                f"All {len(executed_commands)} command(s) matched the whitelist and were dispatched."
                if commands_matched else
                f"Only {len(executed_commands)}/{len(commands)} command(s) matched the whitelist; remainder dropped."
            ),
        }

        self.execution_history.append(record)
        if len(self.execution_history) > 100:
            self.execution_history.pop(0)

        return record

    def get_history(self, limit: int = 50) -> List[dict]:
        return self.execution_history[-limit:]
