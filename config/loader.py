import os
import threading
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class Config:
    def __init__(self, config_path: str = "config/config.yaml"):
        self._path = Path(config_path)
        self._data: dict = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with open(self._path) as f:
            data = yaml.safe_load(f)
        with self._lock:
            self._data = data
        logger.info("Config loaded from %s", self._path)

    def reload(self) -> None:
        self.load()
        logger.info("Config reloaded")

    def get(self, *keys: str, default: Any = None) -> Any:
        # Env var override: nested keys joined by __ in upper case
        # e.g. ("ai_filter", "base_url") → AI_FILTER__BASE_URL
        env_val = os.environ.get("__".join(k.upper() for k in keys))
        if env_val is not None:
            return env_val

        with self._lock:
            node = self._data
            for key in keys:
                if not isinstance(node, dict):
                    return default
                node = node.get(key)
                if node is None:
                    return default
            return node

    # Convenience typed accessors
    def str(self, *keys: str, default: str = "") -> str:
        return str(self.get(*keys, default=default))

    def int(self, *keys: str, default: int = 0) -> int:
        return int(self.get(*keys, default=default))

    def float(self, *keys: str, default: float = 0.0) -> float:
        return float(self.get(*keys, default=default))

    def bool(self, *keys: str, default: bool = False) -> bool:
        v = self.get(*keys, default=default)
        if isinstance(v, bool):
            return v
        return str(v).lower() in ("true", "1", "yes")
