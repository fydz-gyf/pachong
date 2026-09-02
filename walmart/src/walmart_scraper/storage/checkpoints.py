from __future__ import annotations

import json
import logging
from datetime import datetime

from ..config import Settings
from ..utils import atomic_write_json, safe_filename


class CheckpointStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def path(self, keyword: str):
        return self.settings.paths.checkpoints / f"{safe_filename(keyword)}.json"

    @staticmethod
    def initial(keyword: str):
        return {"keyword": keyword, "next_page": 1, "completed": False, "products": [], "last_error": "", "updated_at": ""}

    def load(self, keyword: str):
        if self.settings.force_refresh or not self.settings.resume:
            return self.initial(keyword)
        path = self.path(keyword)
        if not path.exists():
            return self.initial(keyword)
        try:
            cp = json.loads(path.read_text("utf-8"))
            if not isinstance(cp, dict):
                return self.initial(keyword)
            base = self.initial(keyword)
            base.update(cp)
            return base
        except Exception as e:
            logging.warning("[%s] checkpoint read failed: %s", keyword, e)
            return self.initial(keyword)

    def save(self, cp: dict):
        cp["updated_at"] = datetime.now().isoformat(timespec="seconds")
        atomic_write_json(self.path(cp["keyword"]), cp)
