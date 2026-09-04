# SatOps AI — Autonomous Satellite Mission Operations

Multi-agent AI system for real-time satellite anomaly detection,
diagnosis, recovery planning, and runbook generation.

## Architecture

```
Telemetry Stream (simulated LEO satellite)
      │
      ▼
┌─────────────────────┐
│   Monitor Agent     │  Claude Haiku 4.5 — fast threshold + pattern detection
└─────────────────────┘
      │ anomaly detected
      ▼
┌─────────────────────┐
│ Diagnostics Agent   │  Claude Sonnet 5 — root-cause analysis with subsystem KB
└─────────────────────┘
      │ diagnosis
      ▼
┌──────────────────────────────┐
│ Recovery Planner Agent       │  Claude Sonnet 5 — ranked recovery procedures
└──────────────────────────────┘
      │ options
      ▼
┌─────────────────────┐
│    Digital Twin     │  Physics sim + Claude Sonnet 5 — procedure validation
└─────────────────────┘
      │
      ▼
Severity check
LOW/MEDIUM  → Auto-approve
HIGH/CRITICAL → Human approval (dashboard button)
      │
      ▼
┌─────────────────────┐
│ Runbook Generator   │ Claude Sonnet 5 — operator-ready Markdown runbook
└─────────────────────┘
```

---

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run

```bash
python main.py
```

Open **http://localhost:8000** in your browser.

> **No API key? No problem.**
> If no valid `ANTHROPIC_API_KEY` is set,
> the system automatically starts in **offline demo mode**.
> Every agent is powered by a built-in scenario-aware mock LLM
> instead of the Anthropic API.
> The complete pipeline—
> detection → diagnosis → recovery planning →
> digital-twin validation →
> approval routing →
> runbook generation—
> runs end-to-end with **no key,
> no network calls,
> and no cost**.
> A yellow **🧪 DEMO MODE**
> badge appears in the dashboard header.

### 3. (Optional) Use the real Claude models

For live Claude reasoning, provide a real key
(must start with `sk-ant-`):

```bash
# Windows PowerShell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
```

or

```bash
cp .env.example .env
```

Fill in your key.

Then run

```bash
python main.py
```

again.

To force offline mode even if a key exists:

```bash
# Windows PowerShell
$env:SATOPS_OFFLINE = "1"
```

---

## Demo Flow

1. Open the dashboard – watch live telemetry for satellite **LYRA-1**
2. Click **⚡ Inject Anomaly** to choose a failure scenario
3. Watch the agent pipeline execute in real time (right panel)
4. Review the auto-generated diagnosis and recovery options
5. For HIGH/CRITICAL anomalies click **Review & Approve**
6. The generated runbook appears at the bottom of the screen

---

## Anomaly Scenarios

| Scenario | Subsystem | Severity |
|----------|-----------|----------|
| `battery_undervoltage` | EPS | HIGH |
| `thermal_excursion` | THERMAL | HIGH |
| `attitude_drift` | ADCS | MEDIUM |
| `memory_overflow` | OBC | MEDIUM |
| `comms_loss` | COMMS | CRITICAL |

---

## Agent Models

| Agent | Model | Role |
|------|------|------|
| Monitor | Claude Haiku 4.5 | Fast anomaly classification |
| Diagnostics | Claude Sonnet 5 | Root-cause reasoning |
| Recovery Planner | Claude Sonnet 5 | Procedure generation |
| Digital Twin | Claude Sonnet 5 | Outcome validation |
| Runbook Generator | Claude Sonnet 5 | Documentation writing |

---

## API Endpoints

| Method | Path | Description |
|---------|------|-------------|
| GET | `/` | Dashboard UI |
| GET | `/api/status` | Full system state |
| POST | `/api/inject/{scenario}` | Inject a demo anomaly |
| POST | `/api/approve/{anomaly_id}?rank=1` | Approve recovery procedure |
| GET | `/api/runbooks` | List generated runbooks |
| WS | `/ws` | Real-time event stream |

---

## Key Design Decisions

- **All timestamps** use `datetime.now(timezone.utc)` (no hardcoded dates)
- **Auto-approve threshold** configurable via `config.py: AUTO_APPROVE_MAX_SEVERITY`
- **Digital Twin** combines physics rules with Claude validation
- **Runbooks** are stored as Markdown files in `runbooks/`
- **WebSocket** broadcasts every pipeline stage to the dashboard in real time