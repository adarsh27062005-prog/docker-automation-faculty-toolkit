import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from app.config import settings

logger = logging.getLogger(__name__)

CACHE_FILE = Path("scratch/cache_store.json")

class SimpleMemoryCache:
    """In-memory key-value cache with TTL and optional file persistence."""

    def __init__(self, default_ttl: int = settings.CACHE_TTL_SECONDS):
        self.default_ttl = default_ttl
        # key -> (value, expire_timestamp)
        self._store: Dict[str, Tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        if key not in self._store:
            return None
        val, expire_time = self._store[key]
        if time.time() > expire_time:
            del self._store[key]
            return None
        return val

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        effective_ttl = ttl if ttl is not None else self.default_ttl
        expire_time = time.time() + effective_ttl
        self._store[key] = (value, expire_time)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()

cache = SimpleMemoryCache()
