"""
SatOps AI — Autonomous Satellite Mission Operations & Anomaly Response System

Entry point: starts the FastAPI server and the mission orchestrator concurrently.
"""

import asyncio
import os
import sys
import socket

import uvicorn

# The banner and agent activity logs contain Unicode (→ • ⚡ …). Windows
# consoles default to cp1252 and would crash on those characters, so force
# UTF-8 output before anything prints.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

from config import OFFLINE_MODE
from agents.orchestrator import MissionOrchestrator
from api.server import create_app


def is_port_in_use(port):
    """Check if a port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return False
        except OSError:
            return True


async def main():
    port = int(os.getenv("PORT", "8000"))

    # Pre-check port availability
    if is_port_in_use(port):
        print(f"\n[FATAL] Port {port} is already in use.")
        print("\nFix:")
        if sys.platform == "win32":
            print(f"  1. Open PowerShell and run:")
            print(f"     netstat -ano | findstr :{port}")
            print(f"  2. Note the PID in the last column, then:")
            print(f"     taskkill /PID <PID> /F")
        else:
            print(f"  1. Run: lsof -i :{port}")
            print(f"  2. Kill the process: kill -9 <PID>")
        print("\n  OR use a different port by setting PORT env var:")
        print(f"     $env:PORT=8001  # PowerShell")
        print(f"     export PORT=8001  # Bash")
        sys.exit(1)

    if OFFLINE_MODE:
        print("\n[DEMO MODE] No valid ANTHROPIC_API_KEY found — running fully offline.")
        print("             Agents use a built-in scenario-aware mock LLM (no network, no cost).")
        print("             To use the real Claude models, set a key: $env:ANTHROPIC_API_KEY='sk-ant-...'")

    orchestrator = MissionOrchestrator()
    app = create_app(orchestrator)

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )

    server = uvicorn.Server(config)

    print("\n" + "=" * 60)
    print("  SatOps AI — Autonomous Mission Operations System")
    print("=" * 60)
    print(f"  Dashboard   ➜  http://localhost:{port}")
    print(f"  API Docs    ➜  http://localhost:{port}/docs")
    print("=" * 60)

    print("  Agents: Monitor • Diagnostics • Recovery • DigitalTwin • Runbook")

    if OFFLINE_MODE:
        print("  Mode: OFFLINE DEMO (mock LLM — no API key required)")
    else:
        print("  Models: Claude Haiku 4.5 (monitor) + Claude Sonnet 5 (reasoning)")

    print("=" * 60 + "\n")

    server_task = asyncio.create_task(server.serve())
    orchestrator_task = asyncio.create_task(orchestrator.run())

    try:
        done, pending = await asyncio.wait(
            [server_task, orchestrator_task],
            return_when=asyncio.FIRST_EXCEPTION,
        )

        # Check if any task completed with an exception
        for task in done:
            if task.exception():
                raise task.exception()

        # If we get here, wait for remaining tasks
        await asyncio.gather(*pending)

    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Keyboard interrupt received. Cleaning up...")
        server_task.cancel()
        orchestrator_task.cancel()
        try:
            await asyncio.gather(server_task, orchestrator_task, return_exceptions=True)
        except:
            pass
        sys.exit(0)

    except OSError as e:
        if "10048" in str(e) or "Address already in use" in str(e):
            print(f"\n[FATAL] Port {port} bind failed: {e}")
            if sys.platform == "win32":
                print("\nFix:")
                print(f"  netstat -ano | findstr :{port}")
                print(f"  taskkill /PID <PID> /F")
        else:
            print(f"\n[FATAL] {e}")

        server_task.cancel()
        orchestrator_task.cancel()
        try:
            await asyncio.gather(server_task, orchestrator_task, return_exceptions=True)
        except:
            pass
        sys.exit(1)

    except Exception as e:
        print(f"\n[FATAL] Unexpected error: {e}")
        import traceback
        traceback.print_exc()

        server_task.cancel()
        orchestrator_task.cancel()
        try:
            await asyncio.gather(server_task, orchestrator_task, return_exceptions=True)
        except:
            pass
        sys.exit(1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Terminated.")
        sys.exit(0)