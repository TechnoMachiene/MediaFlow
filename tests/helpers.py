"""Test-only stand-in for Gemini. Lives under tests/ so the app can never use it.

`plan` is consumed one entry per call: None = succeed, Exception instance = raise it.
Records (timestamp, sha256) of every call so tests can assert call counts and spacing.
"""
import threading
import time

from app.jobs import JobSpec


class ScriptedClient:
    def __init__(self, plan: list | None = None, delay_s: float = 0.0):
        self.plan = list(plan or [])
        self.delay_s = delay_s
        self.calls: list[tuple[float, str]] = []
        self._lock = threading.Lock()

    def analyse(self, spec: JobSpec) -> dict:
        with self._lock:
            self.calls.append((time.monotonic(), spec.sha256))
            step = self.plan.pop(0) if self.plan else None
        if self.delay_s:
            time.sleep(self.delay_s)
        if isinstance(step, Exception):
            raise step
        return {"text": f"scripted analysis of {spec.sha256[:8]}", "kind": spec.kind,
                "model": spec.model, "tokens": 0}
