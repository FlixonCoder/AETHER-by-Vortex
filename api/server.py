"""
FastAPI Server Endpoints
"""
import asyncio
import json
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
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
    async def get_history(param: str, n: int = 20): return {"param": param, "history": orchestrator.simulator.get_history(param, n)}

    @app.get("/api/runbooks")
    async def list_runbooks(): return {"runbooks": orchestrator.self_runbooks}

    @app.get("/api/runbooks/{filename}")
    async def get_runbook(filename: str):
        path = Path("runbooks") / filename
        if not path.exists(): raise HTTPException(status_code=404, detail="Runbook not found")
        return {"content": path.read_text(encoding="utf-8")}

    @app.post("/api/inject/{scenario}")
    async def inject_anomaly(scenario: str):
        if scenario not in ANOMALY_SCENARIOS: raise HTTPException(status_code=400, detail="Unknown scenario.")
        return {"injected": scenario, "info": await orchestrator.inject_anomaly(scenario)}

    @app.post("/api/approve/{anomaly_id}")
    async def approve(anomaly_id: str, rank: int = 1):
        if not await orchestrator.approve_procedure(anomaly_id, rank): raise HTTPException(status_code=404, detail="Not found.")
        return {"approved": anomaly_id, "rank": rank}

    @app.get("/api/scenarios")
    async def get_scenarios(): return {k: {"subsystem": v["subsystem"], "severity": v["severity"], "description": v["description"]} for k, v in ANOMALY_SCENARIOS.items()}

    return app