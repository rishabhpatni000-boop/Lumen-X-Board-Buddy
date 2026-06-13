#!/usr/bin/env python3
"""Small in-memory TTL cache for duplicate request suppression."""

from __future__ import annotations

import hashlib
import json
import threading
import time


class TTLCache:
    def __init__(self, ttl_seconds: int = 600, max_entries: int = 128):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._store = {}
        self._lock = threading.Lock()

    def _purge(self):
        now = time.time()
        expired = [key for key, (_, expires_at) in self._store.items() if expires_at <= now]
        for key in expired:
            self._store.pop(key, None)
        while len(self._store) > self.max_entries:
            oldest_key = min(self._store, key=lambda key: self._store[key][1])
            self._store.pop(oldest_key, None)

    def get(self, key: str):
        with self._lock:
            self._purge()
            hit = self._store.get(key)
            return hit[0] if hit else None

    def set(self, key: str, value):
        with self._lock:
            self._purge()
            self._store[key] = (value, time.time() + self.ttl_seconds)


def stable_cache_key(prefix: str, payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"
