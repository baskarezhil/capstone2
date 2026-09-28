"""Append-only, hash-chained audit log.

Each record stores the SHA-256 of the previous record, so any edit or deletion breaks the
chain (`verify_chain`). Production: ship the same records to S3 with Object Lock (WORM).
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from opentelemetry import trace

from .config import SETTINGS

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return GENESIS
        last = GENESIS
        with self.path.open("rb") as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["hash"]
        return last

    def record(self, incident_id: str, actor: str, event: str, **details: Any) -> dict[str, Any]:
        with self._lock:
            span = trace.get_current_span().get_span_context()
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "incident_id": incident_id,
                "actor": actor,
                "event": event,
                "details": details,
                "trace_id": format(span.trace_id, "032x") if span.is_valid else None,
                "prev_hash": self._last_hash(),
            }
            entry["hash"] = hashlib.sha256(
                json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            return entry

    def entries(self, incident_id: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if incident_id is None or r["incident_id"] == incident_id]

    def verify_chain(self) -> tuple[bool, int, str]:
        prev = GENESIS
        rows = self.entries()
        for i, row in enumerate(rows):
            body = {k: v for k, v in row.items() if k != "hash"}
            if row["prev_hash"] != prev:
                return False, i, "prev_hash mismatch (record removed or reordered)"
            if hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest() != row["hash"]:
                return False, i, "hash mismatch (record modified)"
            prev = row["hash"]
        return True, len(rows), "ok"


AUDIT = AuditLog(SETTINGS.audit_path)
