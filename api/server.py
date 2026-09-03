"""
FastAPI Server Endpoints
"""
import asyncio
import json
from pathlib import Path
from typing import Optional, Set

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from agents.orchestrator import MissionOrchestrator
from api.space_data import router as space_router
from config import ANOMALY_SCENARIOS

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
            try: await ws.send_text(text)
            except Exception: dead.add(ws)
        self._active -= dead


def create_app(orchestrator: MissionOrchestrator) -> FastAPI:
    app = FastAPI(title="Satellite Mission Ops AI", version="1.0.0")
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
            await websocket.send_text(json.dumps({"type": "connected", "data": orchestrator.get_status()}))
            while True: await websocket.receive_text()
        except WebSocketDisconnect: manager.disconnect(websocket)

    @app.get("/", response_class=HTMLResponse)
    async def landing():
        return HTMLResponse(content=(static_dir / "landing.html").read_text(encoding="utf-8"))

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(content=(static_dir / "index.html").read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def get_status(): return orchestrator.get_status()

    @app.get("/api/history/{param}")
    async def get_history(param: str, n: int = 20, sat_id: Optional[str] = None):
        return {"param": param, "sat_id": sat_id,
                "history": orchestrator.get_simulator(sat_id).get_history(param, n)}

    @app.get("/api/fleet")
    async def get_fleet(): return orchestrator.fleet_status()

    @app.get("/api/clusters")
    async def get_clusters():
        """Current formations, their hosts, and each host's failover state."""
        return orchestrator.clusters.status()

    @app.get("/api/stages")
    async def get_stages():
        """Counters for the three processing tiers and the triage queue."""
        return {**orchestrator._stage_counts,
                "queue_depth": orchestrator._triage.qsize()}

    @app.post("/api/fleet/adopt")
    async def adopt_satellite(payload: dict = Body(...)):
        """Bring a satellite picked on the orbit map under mission control."""
        sat_id = str(payload.get("sat_id") or "").strip()
        name = str(payload.get("name") or sat_id).strip()
        if not sat_id:
            raise HTTPException(status_code=400, detail="sat_id is required.")

        def _num(key):
            v = payload.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        identity = orchestrator.adopt_satellite(
            sat_id=sat_id, name=name, norad_id=payload.get("norad_id"),
            altitude_km=_num("altitude_km"), inclination_deg=_num("inclination_deg"),
            period_min=_num("period_min"),
            mission=str(payload.get("mission") or "imaging"),
            raan_deg=_num("raan_deg"),
        )
        if payload.get("activate", True):
            orchestrator.set_active(sat_id)
        return {"adopted": identity, "fleet": orchestrator.fleet_status()}

    @app.post("/api/fleet/active/{sat_id}")
    async def set_active(sat_id: str):
        if not orchestrator.set_active(sat_id):
            raise HTTPException(status_code=404, detail="Satellite not under mission control.")
        return orchestrator.fleet_status()

    @app.get("/api/runbooks")
    async def list_runbooks(): return {"runbooks": orchestrator.self_runbooks}

    @app.get("/api/runbooks/{filename}")
    async def get_runbook(filename: str):
        path = Path("runbooks") / filename
        if not path.exists(): raise HTTPException(status_code=404, detail="Runbook not found")
        return {"content": path.read_text(encoding="utf-8")}

    @app.post("/api/inject/{scenario}")
    async def inject_anomaly(scenario: str, sat_id: Optional[str] = None):
        """Inject a fault. Defaults to whichever satellite is currently selected."""
        if scenario not in ANOMALY_SCENARIOS: raise HTTPException(status_code=400, detail="Unknown scenario.")
        return {"injected": scenario, "info": await orchestrator.inject_anomaly(scenario, sat_id)}

    @app.post("/api/approve/{anomaly_id}")
    async def approve(anomaly_id: str, rank: int = 1):
        if not await orchestrator.approve_procedure(anomaly_id, rank): raise HTTPException(status_code=404, detail="Not found.")
        return {"approved": anomaly_id, "rank": rank}

    @app.get("/api/scenarios")
    async def get_scenarios(): return {k: {"subsystem": v["subsystem"], "severity": v["severity"], "description": v["description"]} for k, v in ANOMALY_SCENARIOS.items()}

    return app