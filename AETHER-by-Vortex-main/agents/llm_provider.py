"""
3-Tier LLM Provider with Real-Time Probing and Autonomous Fallback.
Tier 1: LOCAL  -> Ollama (Auto-detects installed models, e.g. qwen3.5:4b)
Tier 2: CLOUD  -> Groq (Llama 3.3 / Llama 3)
Tier 3: SAFETY -> Deterministic Rule Engine

Continuously probes local & cloud availability in real-time,
dynamically reflecting whichever tier is currently online.
"""
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv

from .rule_engine import DeterministicRuleEngine


def safe_number(value: Any, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    """Coerces an LLM-produced numeric field to a float, clamped to [lo, hi].

    Every numeric field an LLM tier emits (confidence, risk_score, recovery
    probability, ...) is untrusted input: it can arrive as a string, an
    out-of-range number, NaN, or simply be missing. A bare float()/int() cast
    on that value raises on anything non-numeric and silently corrupts safety
    decisions on anything out-of-range — either failure abandons the incident
    or, worse, feeds a bogus number straight into a safety gate. This is the
    one place every agent should route such casts through.
    """
    try:
        num = float(value)
        if num != num:  # NaN
            raise ValueError("NaN")
    except (TypeError, ValueError):
        return default
    if lo is not None:
        num = max(lo, num)
    if hi is not None:
        num = min(hi, num)
    return num


def _extract_json(text: str) -> Optional[dict]:
    """Robust extractor for JSON objects from LLM outputs."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    match = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    return None


class LLMProvider:
    """Manages the 3-tier fallback execution pipeline with real-time health probing."""

    def __init__(self):
        self.rule_engine = DeterministicRuleEngine()
        self._current_mode: str = "RULE_BASED"
        self._mode_details: str = "Initializing availability probe..."
        self._last_error: Optional[str] = None

        self._ollama_alive: bool = False
        self._groq_alive: bool = False
        self._active_ollama_model: str = "qwen3.5:4b"

        # Initial fast sync probe
        self._sync_load_env()

    def _sync_load_env(self):
        """Reloads .env in real time."""
        try:
            load_dotenv(override=True)
        except Exception:
            pass

    @property
    def current_mode(self) -> str:
        return self._current_mode

    @property
    def active_model(self) -> str:
        if self._current_mode == "LOCAL":
            return self._active_ollama_model
        elif self._current_mode == "CLOUD":
            return os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        return "Deterministic Rule Engine"

    def get_mode_display(self) -> str:
        if self._current_mode == "LOCAL":
            return f"LOCAL — Ollama / {self._active_ollama_model}"
        elif self._current_mode == "CLOUD":
            groq_m = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
            return f"CLOUD — Groq / {groq_m}"
        else:
            return "RULE BASED — Fallback Engine"

    def get_mode_info(self) -> dict:
        return {
            "mode": self._current_mode,
            "display": self.get_mode_display(),
            "details": self._mode_details,
            "ollama_alive": self._ollama_alive,
            "groq_alive": self._groq_alive,
            "active_model": self.active_model,
            "ollama_model": self._active_ollama_model,
            "groq_model": os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile"),
            "groq_configured": bool(os.getenv("GROQ_API_KEY", "").strip())
        }

    async def probe_availability(self) -> str:
        """
        Actively probes Ollama & Groq endpoints to determine the active AI mode in real-time.
        Returns the resolved mode ("LOCAL", "CLOUD", or "RULE_BASED").
        """
        self._sync_load_env()
        force_rule = os.getenv("FORCE_RULE_ENGINE", "").lower() in ("1", "true", "yes", "on")
        if force_rule:
            self._current_mode = "RULE_BASED"
            self._mode_details = "FORCE_RULE_ENGINE is enabled in .env"
            return self._current_mode

        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        configured_ollama_model = os.getenv("OLLAMA_MODEL", "qwen3.5:4b").strip()
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        groq_base = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")

        # 1. Probe Local Ollama
        ollama_ok = False
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(f"{ollama_url}/api/tags")
                if resp.status_code == 200:
                    installed_models = [m.get("name", "") for m in resp.json().get("models", [])]
                    if installed_models:
                        ollama_ok = True
                        # Auto-match configured model if installed
                        matched = [m for m in installed_models if configured_ollama_model in m]
                        if matched:
                            self._active_ollama_model = matched[0]
                        else:
                            # Use first available model (e.g. qwen3.5:4b)
                            self._active_ollama_model = installed_models[0]
        except Exception:
            ollama_ok = False

        self._ollama_alive = ollama_ok

        # 2. Probe Cloud Groq
        groq_ok = False
        if groq_key and len(groq_key) > 15 and not groq_key.startswith("your_"):
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.get(
                        f"{groq_base}/models",
                        headers={"Authorization": f"Bearer {groq_key}"}
                    )
                    if resp.status_code == 200:
                        groq_ok = True
            except Exception:
                groq_ok = False

        self._groq_alive = groq_ok

        # 3. Hierarchy Decision
        old_mode = self._current_mode
        if self._ollama_alive:
            self._current_mode = "LOCAL"
            self._mode_details = f"Local Ollama online ({self._active_ollama_model})"
        elif self._groq_alive:
            self._current_mode = "CLOUD"
            self._mode_details = f"Groq cloud online ({os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')})"
        else:
            self._current_mode = "RULE_BASED"
            self._mode_details = "Deterministic satellite rule engine active"

        return self._current_mode

    async def generate_json(
        self,
        prompt: str,
        system_instruction: str,
        agent_role: str,
        fallback_handler,
        timeout: Optional[float] = None
    ) -> Tuple[dict, str]:
        """
        Attempts execution across the 3 tiers with automatic fallback.
        Returns: (parsed_json_dict, mode_used)
        """
        # Re-probe mode before generation
        await self.probe_availability()

        force_rule = os.getenv("FORCE_RULE_ENGINE", "").lower() in ("1", "true", "yes", "on")
        if force_rule:
            self._current_mode = "RULE_BASED"
            return fallback_handler(), "RULE_BASED"

        # -------------------------------------------------------------
        # Tier 1: Local Ollama (if alive)
        # -------------------------------------------------------------
        if self._ollama_alive:
            try:
                ollama_timeout = float(os.getenv("OLLAMA_TIMEOUT", "30.0"))
                ollama_res = await self._call_ollama(prompt, system_instruction, timeout or ollama_timeout)
                parsed = _extract_json(ollama_res)
                if parsed and isinstance(parsed, dict):
                    self._current_mode = "LOCAL"
                    return parsed, "LOCAL"
                else:
                    print(f"[{agent_role}] Ollama returned invalid JSON. Falling back to Groq.", flush=True)
            except Exception as e:
                self._last_error = f"Ollama: {type(e).__name__}: {e}"
                print(f"[{agent_role}] Ollama failed ({e}). Falling back to Groq.", flush=True)

        # -------------------------------------------------------------
        # Tier 2: Cloud Groq (if alive or key present)
        # -------------------------------------------------------------
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        if groq_key and len(groq_key) > 15 and not groq_key.startswith("your_"):
            try:
                groq_timeout = float(os.getenv("GROQ_TIMEOUT", "20.0"))
                groq_res = await self._call_groq(prompt, system_instruction, timeout or groq_timeout)
                parsed = _extract_json(groq_res)
                if parsed and isinstance(parsed, dict):
                    self._current_mode = "CLOUD"
                    return parsed, "CLOUD"
                else:
                    print(f"[{agent_role}] Groq returned invalid JSON. Falling back to Rule Engine.", flush=True)
            except Exception as e:
                self._last_error = f"Groq: {type(e).__name__}: {e}"
                print(f"[{agent_role}] Groq failed ({e}). Falling back to Rule Engine.", flush=True)

        # -------------------------------------------------------------
        # Tier 3: Deterministic Rule Engine
        # -------------------------------------------------------------
        self._current_mode = "RULE_BASED"
        return fallback_handler(), "RULE_BASED"

    async def _call_ollama(self, prompt: str, system: str, timeout: float) -> str:
        """Invokes local Ollama server using httpx."""
        ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
        timeout_cfg = httpx.Timeout(timeout, connect=3.0)
        async with httpx.AsyncClient(timeout=timeout_cfg) as client:
            payload = {
                "model": self._active_ollama_model,
                "messages": [
                    {"role": "system", "content": system + "\nRespond with valid JSON only."},
                    {"role": "user", "content": prompt}
                ],
                "stream": False,
                "options": {
                    "temperature": 0.1
                }
            }
            resp = await client.post(f"{ollama_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "")

    async def _call_groq(self, prompt: str, system: str, timeout: float) -> str:
        """Invokes Groq OpenAI-compatible chat completion API using httpx."""
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        groq_base = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
        candidate_models = [
            os.getenv("GROQ_MODEL", "openai/gpt-oss-20b").strip(),
            "openai/gpt-oss-20b",
            "qwen/qwen3.8-27b",
            "groq/compound-mini",
            "openai/gpt-oss-120b"
        ]
        # De-duplicate while preserving order
        candidate_models = list(dict.fromkeys(m for m in candidate_models if m))

        async with httpx.AsyncClient(timeout=timeout) as client:
            headers = {
                "Authorization": f"Bearer {groq_key}",
                "Content-Type": "application/json"
            }
            last_err = None
            for model_name in candidate_models:
                payload = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt}
                    ],
                    "temperature": 0.1
                }
                try:
                    resp = await client.post(f"{groq_base}/chat/completions", headers=headers, json=payload)
                    if resp.status_code == 200:
                        data = resp.json()
                        return data["choices"][0]["message"]["content"]
                    elif resp.status_code == 404:
                        # Model not found on this account, try next candidate
                        continue
                    else:
                        resp.raise_for_status()
                except Exception as e:
                    last_err = e
            if last_err:
                raise last_err
            raise RuntimeError("No available Groq model succeeded")
