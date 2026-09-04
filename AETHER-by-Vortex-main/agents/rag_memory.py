"""
RAG Memory System for Autonomous Satellite Mission Operations.
Provides pure-Python persistent Episodic & Procedural memory using TF-IDF and Cosine Similarity.
Zero external heavy dependencies (no PyTorch, ChromaDB, or sentence-transformers).
"""
import json
import math
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import MEMORY_DIR


def _tokenize(text: str) -> List[str]:
    """Tokenize and normalize text into alphanumeric words."""
    if not text:
        return []
    words = re.findall(r"\b[a-zA-Z0-9_-]{2,}\b", text.lower())
    # Drop very common stop words
    stops = {
        "the", "and", "is", "in", "to", "of", "for", "with", "on", "at", "by", "from",
        "an", "be", "this", "that", "which", "or", "as", "are", "was", "were", "it"
    }
    return [w for w in words if w not in stops]


class TFIDFIndex:
    """Lightweight pure-Python TF-IDF Vector Index with Cosine Similarity."""

    def __init__(self):
        self.doc_ids: List[str] = []
        self.doc_tokens: List[List[str]] = []
        self.doc_store: Dict[str, dict] = {}
        self.idf: Dict[str, float] = {}

    def fit_and_index(self, documents: List[Tuple[str, str, dict]]):
        """
        documents: List of (doc_id, text_to_index, full_payload)
        """
        self.doc_ids = []
        self.doc_tokens = []
        self.doc_store = {}
        self.idf = {}

        if not documents:
            return

        total_docs = len(documents)
        df_counter = Counter()

        for doc_id, text, payload in documents:
            tokens = _tokenize(text)
            self.doc_ids.append(doc_id)
            self.doc_tokens.append(tokens)
            self.doc_store[doc_id] = payload
            unique_tokens = set(tokens)
            for t in unique_tokens:
                df_counter[t] += 1

        for term, df in df_counter.items():
            # Standard smoothed inverse document frequency
            self.idf[term] = math.log((1.0 + total_docs) / (1.0 + df)) + 1.0

    def query(self, query_text: str, top_k: int = 3) -> List[Tuple[dict, float]]:
        """Return top_k documents with similarity scores (0.0 - 1.0)."""
        if not self.doc_ids:
            return []

        q_tokens = _tokenize(query_text)
        if not q_tokens:
            return []

        q_tf = Counter(q_tokens)
        # Compute query vector
        q_vec = {t: tf * self.idf.get(t, 1.0) for t, tf in q_tf.items()}
        q_norm = math.sqrt(sum(v * v for v in q_vec.values()))
        if q_norm == 0:
            return []

        scores = []
        for idx, doc_id in enumerate(self.doc_ids):
            tokens = self.doc_tokens[idx]
            if not tokens:
                continue
            doc_tf = Counter(tokens)
            doc_vec = {t: tf * self.idf.get(t, 1.0) for t, tf in doc_tf.items()}
            doc_norm = math.sqrt(sum(v * v for v in doc_vec.values()))
            if doc_norm == 0:
                continue

            # Dot product
            dot = sum(q_vec[t] * doc_vec.get(t, 0.0) for t in q_vec)
            sim = dot / (q_norm * doc_norm)
            if sim > 0.01:
                scores.append((self.doc_store[doc_id], round(sim, 4)))

        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


class RAGMemory:
    """
    Episodic and Procedural memory repository.
    Stores and retrieves incident reports and procedural knowledge.
    """

    def __init__(self, memory_dir: Optional[Path] = None):
        self.memory_dir = memory_dir or MEMORY_DIR
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.incidents_file = self.memory_dir / "incidents.jsonl"
        self.procedures_file = self.memory_dir / "procedures.jsonl"

        self.incident_index = TFIDFIndex()
        self.procedure_index = TFIDFIndex()

        self._load_and_index()

    def _load_and_index(self):
        """Loads all entries from jsonl files and rebuilds indices."""
        # Check if procedures file exists; if empty, seed initial procedural knowledge
        if not self.procedures_file.exists() or self.procedures_file.stat().st_size == 0:
            self._seed_procedural_memory()

        # Index incidents
        incidents = self.get_all_incidents()
        inc_docs = []
        for inc in incidents:
            text = f"{inc.get('anomaly', '')} {inc.get('root_cause', '')} {inc.get('subsystem', '')} " \
                   f"{' '.join(inc.get('lessons_learned', []))} {inc.get('solution', '')}"
            inc_docs.append((inc["incident_id"], text, inc))
        self.incident_index.fit_and_index(inc_docs)

        # Index procedures
        procs = self.get_all_procedures()
        proc_docs = []
        for pr in procs:
            text = f"{pr.get('procedure_id', '')} {pr.get('name', '')} {pr.get('subsystem', '')} " \
                   f"{pr.get('anomaly_pattern', '')} {' '.join(pr.get('steps', []))} {pr.get('description', '')}"
            proc_docs.append((pr["procedure_id"], text, pr))
        self.procedure_index.fit_and_index(proc_docs)

    def _seed_procedural_memory(self):
        """Pre-populate default operational procedures for satellite subsystems."""
        seeds = [
            {
                "procedure_id": "PROC-EPS-01",
                "subsystem": "EPS",
                "anomaly_pattern": "battery undervoltage low state of charge solar efficiency degradation",
                "name": "Load Shedding & Solar Power Recovery",
                "description": "Reduces bus power demand and recalibrates MPPT to prioritize battery recharge during insolation.",
                "commands": [
                    {"command": "LOAD_SHED_NON_ESSENTIAL", "parameters": {"level": "standard"}},
                    {"command": "MPPT_RECALIBRATE", "parameters": {}},
                    {"command": "ATTITUDE_HOLD_SUN", "parameters": {"bias_deg": 0.0}}
                ],
                "expected_outcome": "Battery charging rate increases by >2.5A, bus voltage stabilizes above 26.5V",
                "risk": "LOW",
                "success_rate": 0.94,
                "reversible": True
            },
            {
                "procedure_id": "PROC-ADCS-01",
                "subsystem": "ADCS",
                "anomaly_pattern": "reaction wheel overload friction saturation attitude drift pointing error",
                "name": "Reaction Wheel Magnetorquer Desaturation",
                "description": "Uses Earth magnetic field via magnetorquer coils to desaturate accumulated angular momentum.",
                "commands": [
                    {"command": "REACTION_WHEEL_DESAT", "parameters": {"target_wheel_rpm": 2000.0}}
                ],
                "expected_outcome": "Reaction wheel RPM drops below 2500 RPM, attitude error returns <0.5 deg",
                "risk": "LOW",
                "success_rate": 0.96,
                "reversible": True
            },
            {
                "procedure_id": "PROC-THERMAL-01",
                "subsystem": "THERMAL",
                "anomaly_pattern": "thermal excursion high temperature runaway stuck heater relay battery bay",
                "name": "Heater Relay Forced Cycle & Radiator Slew",
                "description": "Cycles stuck heater relays and slews spacecraft to deep space cold side to dump excess heat.",
                "commands": [
                    {"command": "HEATER_RELAY_CYCLE", "parameters": {"relay_id": "BAY_1"}},
                    {"command": "RADIATOR_SLEW_BIAS", "parameters": {"bias_angle": 15.0}}
                ],
                "expected_outcome": "Battery and payload temperatures decrease toward nominal 20-25 deg C band",
                "risk": "MEDIUM",
                "success_rate": 0.91,
                "reversible": True
            },
            {
                "procedure_id": "PROC-OBC-01",
                "subsystem": "OBC",
                "anomaly_pattern": "memory overflow high cpu payload buffer leak task starvation watchdog",
                "name": "Payload Buffer Flush & Task Soft Restart",
                "description": "Preserves critical science states to flash then flushes the corrupt volatile buffer.",
                "commands": [
                    {"command": "PAYLOAD_BUFFER_FLUSH", "parameters": {"backup_flash": True}},
                    {"command": "OBC_SOFT_RESTART_TASK", "parameters": {"task_name": "science_telemetry"}}
                ],
                "expected_outcome": "Memory usage drops below 55%, CPU utilization returns to <35%",
                "risk": "LOW",
                "success_rate": 0.95,
                "reversible": True
            },
            {
                "procedure_id": "PROC-COMMS-01",
                "subsystem": "COMMS",
                "anomaly_pattern": "comms loss downlink snr drop antenna mispoint gimbal jam link margin violation",
                "name": "Antenna Gimbal Rehoming & UHF Failover",
                "description": "Switches immediately to omni UHF telemetry link while rehoming the main S-band gimbal.",
                "commands": [
                    {"command": "COMMS_UHF_FAILOVER", "parameters": {}},
                    {"command": "ANTENNA_GIMBAL_REHOME", "parameters": {"axis": "all"}}
                ],
                "expected_outcome": "Downlink SNR restores above 18 dB threshold",
                "risk": "MEDIUM",
                "success_rate": 0.88,
                "reversible": True
            },
            {
                "procedure_id": "PROC-SAFE-01",
                "subsystem": "OBC",
                "anomaly_pattern": "critical cascade unrecoverable multi-subsystem failure safe mode hold",
                "name": "Spacecraft Safe Mode Hold",
                "description": "Preserves vehicle survivability by powering down non-critical buses and holding sun-pointing.",
                "commands": [
                    {"command": "SAFE_MODE_ENTER", "parameters": {}},
                    {"command": "ATTITUDE_HOLD_SUN", "parameters": {"bias_deg": 0.0}}
                ],
                "expected_outcome": "Spacecraft enters stable power-positive minimal operational state",
                "risk": "LOW",
                "success_rate": 0.99,
                "reversible": True
            }
        ]

        with open(self.procedures_file, "w", encoding="utf-8") as f:
            for item in seeds:
                f.write(json.dumps(item) + "\n")

    def get_all_incidents(self) -> List[dict]:
        """Reads all incidents from the incidents JSONL file."""
        if not self.incidents_file.exists():
            return []
        incidents = []
        with open(self.incidents_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        incidents.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return incidents

    def get_all_procedures(self) -> List[dict]:
        """Reads all procedural records from the procedures JSONL file."""
        if not self.procedures_file.exists():
            return []
        procedures = []
        with open(self.procedures_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        procedures.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return procedures

    def store_incident(self, report: dict) -> str:
        """
        Store a structured incident report and update the vector index.
        Returns incident_id.
        """
        if "incident_id" not in report:
            report["incident_id"] = f"INC-{uuid.uuid4().hex[:8].upper()}"
        if "timestamp" not in report:
            report["timestamp"] = datetime.now(timezone.utc).isoformat()

        # Append to JSONL
        with open(self.incidents_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(report) + "\n")

        # Update index
        self._load_and_index()
        return report["incident_id"]

    def retrieve_similar_incidents(self, query_text: str, k: int = 3) -> List[dict]:
        """Retrieve top-K matching historical incidents with similarity scores."""
        results = self.incident_index.query(query_text, top_k=k)
        formatted = []
        for doc, score in results:
            item = dict(doc)
            item["similarity_score"] = score
            formatted.append(item)
        return formatted

    def retrieve_procedures(self, subsystem: str, query_text: str = "", k: int = 3) -> List[dict]:
        """Retrieve matching procedural knowledge filtered or ranked by subsystem."""
        all_procs = self.get_all_procedures()
        # Direct subsystem match candidates
        subsys_matches = [p for p in all_procs if p.get("subsystem", "").upper() == subsystem.upper()]
        
        if query_text:
            query_results = self.procedure_index.query(f"{subsystem} {query_text}", top_k=k*2)
            ranked_ids = {doc["procedure_id"]: score for doc, score in query_results}
            # Prioritize subsystem matches then ranking
            subsys_matches.sort(key=lambda p: ranked_ids.get(p["procedure_id"], 0.0), reverse=True)

        return subsys_matches[:k] if subsys_matches else all_procs[:k]

    def get_stats(self) -> dict:
        """Returns statistics on memory contents."""
        incidents = self.get_all_incidents()
        procedures = self.get_all_procedures()
        recovered_count = sum(1 for inc in incidents if inc.get("outcome") == "RECOVERED")
        return {
            "total_incidents": len(incidents),
            "recovered_incidents": recovered_count,
            "total_procedures": len(procedures),
            "last_updated": datetime.now(timezone.utc).isoformat()
        }
