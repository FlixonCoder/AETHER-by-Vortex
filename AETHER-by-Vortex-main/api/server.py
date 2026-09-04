"""
FastAPI Server Endpoints for AETHER Satellite Mission Ops.
"""
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from agents.orchestrator import MissionOrchestrator
from api.space_data import router as space_router
from config import ANOMALY_SCENARIOS, RUNBOOK_DIR


class ConnectionManager:
    def __init__(self):
        self._active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._active.add(ws)

    def disconnect(self, ws: WebSocket):
        self._active.discard(ws)

    async def broadcast(self, text: str):
        dead = set()
        for ws in self._active:
            try:
                await ws.send_text(text)
            except Exception:
                dead.add(ws)
        self._active -= dead


def create_app(orchestrator: MissionOrchestrator) -> FastAPI:
    app = FastAPI(title="AETHER — Autonomous Satellite Mission Ops AI", version="2.0.0")
    manager = ConnectionManager()

    static_dir = Path(__file__).parent.parent / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(space_router)

    async def broadcast_cb(text: str):
        await manager.broadcast(text)

    orchestrator.set_broadcast_callback(broadcast_cb)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await manager.connect(websocket)
        try:
            # Send initial state
            await websocket.send_text(json.dumps({"type": "connected", "data": orchestrator.get_status()}))
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "ping":
                        await websocket.send_text(json.dumps({"type": "pong", "timestamp": orchestrator.simulator.orbital_context()}))
                except Exception:
                    pass
        except WebSocketDisconnect:
            manager.disconnect(websocket)

    # -------------------------------------------------------------
    # HTML Views
    # -------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def landing():
        return HTMLResponse(content=(static_dir / "landing.html").read_text(encoding="utf-8"))

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(content=(static_dir / "index.html").read_text(encoding="utf-8"))

    # -------------------------------------------------------------
    # Status & Telemetry
    # -------------------------------------------------------------
    @app.get("/api/status")
    async def get_status():
        return orchestrator.get_status()

    @app.get("/api/llm-mode")
    async def get_llm_mode():
        await orchestrator.llm_provider.probe_availability()
        return orchestrator.llm_provider.get_mode_info()

    @app.get("/api/history/{param}")
    async def get_history(param: str, n: int = 20):
        return {"param": param, "history": orchestrator.simulator.get_history(param, n)}

    # -------------------------------------------------------------
    # Anomaly Injection & Approval Controls
    # -------------------------------------------------------------
    @app.get("/api/scenarios")
    async def get_scenarios():
        return {
            k: {
                "subsystem": v["subsystem"],
                "severity": v["severity"],
                "description": v["description"]
            }
            for k, v in ANOMALY_SCENARIOS.items()
        }

    @app.post("/api/inject/{scenario}")
    async def inject_anomaly(scenario: str):
        if scenario not in ANOMALY_SCENARIOS:
            raise HTTPException(status_code=400, detail=f"Unknown scenario '{scenario}'.")
        info = await orchestrator.inject_anomaly(scenario)
        return {"injected": scenario, "info": info}

    @app.post("/api/approve/{anomaly_id}")
    async def approve_anomaly(anomaly_id: str, action_id: Optional[str] = None):
        success = await orchestrator.approve_procedure(anomaly_id, action_id=action_id)
        if not success:
            raise HTTPException(status_code=404, detail="Incident not found or already processed.")
        return {"approved": True, "anomaly_id": anomaly_id, "action_id": action_id}

    @app.post("/api/reject/{anomaly_id}")
    async def reject_anomaly(anomaly_id: str, reason: str = "Operator manual denial"):
        success = await orchestrator.reject_procedure(anomaly_id, reason=reason)
        if not success:
            raise HTTPException(status_code=404, detail="Incident not found or already processed.")
        return {"rejected": True, "anomaly_id": anomaly_id, "reason": reason}

    # -------------------------------------------------------------
    # RAG Memory & Learning Loop Endpoints
    # -------------------------------------------------------------
    @app.get("/api/memory/incidents")
    async def get_rag_incidents(q: Optional[str] = None, k: int = 10):
        if q:
            results = orchestrator.rag_memory.retrieve_similar_incidents(q, k=k)
        else:
            results = orchestrator.rag_memory.get_all_incidents()[-k:]
        return {"count": len(results), "incidents": results}

    @app.get("/api/memory/procedures")
    async def get_rag_procedures(subsystem: Optional[str] = None):
        if subsystem:
            procs = orchestrator.rag_memory.retrieve_procedures(subsystem=subsystem, k=10)
        else:
            procs = orchestrator.rag_memory.get_all_procedures()
        return {"count": len(procs), "procedures": procs}

    @app.get("/api/memory/stats")
    async def get_rag_stats():
        return orchestrator.rag_memory.get_stats()

    # -------------------------------------------------------------
    # Audit Logs & Execution History
    # -------------------------------------------------------------
    @app.get("/api/audit")
    async def get_audit_logs(limit: int = 50, incident_id: Optional[str] = None):
        entries = orchestrator.audit_logger.get_entries(limit=limit, incident_id=incident_id)
        return {"count": len(entries), "audit_logs": entries}

    @app.get("/api/execution-log")
    async def get_execution_log(limit: int = 50):
        history = orchestrator.executor.get_history(limit=limit)
        return {"count": len(history), "executions": history}

    # -------------------------------------------------------------
    # Rolling Telemetry Analysis
    # -------------------------------------------------------------
    @app.get("/api/telemetry/rolling-analysis")
    async def get_rolling_analysis():
        """Return current rolling window stats for all tracked parameters."""
        stats = orchestrator.rolling_analyzer.get_summary()
        return {
            "count": len(stats),
            "window_seconds": orchestrator.rolling_analyzer._window_seconds,
            "stats": stats,
        }

    @app.post("/api/inject-spike/{param}")
    async def inject_spike(param: str, magnitude: float = 5.0, duration_ticks: int = 1):
        """
        Inject a transient single-parameter spike for rolling-filter testing.
        Uses the simulator's internal state to briefly push the parameter.
        """
        from config import TELEMETRY_PARAMS
        if param not in TELEMETRY_PARAMS:
            raise HTTPException(status_code=400, detail=f"Unknown parameter '{param}'.")
        from datetime import datetime, timezone
        # Directly inject a temporary ad-hoc anomaly
        sim = orchestrator.simulator
        if sim._active_anomaly is None:
            sim._active_anomaly = {
                "subsystem": TELEMETRY_PARAMS[param]["subsystem"],
                "severity": "LOW",
                "deltas": {param: magnitude},
                "description": f"Injected transient spike on {param}",
                "key": "__spike__",
                "remaining_ticks": duration_ticks,
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
        return {"injected_spike": param, "magnitude": magnitude, "duration_ticks": duration_ticks}


    @app.get("/api/runbooks")
    async def list_runbooks():
        return {"runbooks": orchestrator.runbooks}

    @app.get("/api/runbooks/{filename}")
    async def get_runbook(filename: str):
        path = RUNBOOK_DIR / filename
        if not path.exists():
            raise HTTPException(status_code=404, detail="Runbook not found")
        return {"filename": filename, "content": path.read_text(encoding="utf-8")}

    return app