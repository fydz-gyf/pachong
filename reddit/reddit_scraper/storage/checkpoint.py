from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from reddit_scraper.models import Loader


def loader_key(loader: Loader) -> str:
    raw = (loader.src + "\n" + loader.cursor).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


@dataclass
class ResumeState:
    completed: bool = False
    processed_loader_keys: set[str] = field(default_factory=set)
    pending_top: deque[Loader] = field(default_factory=deque)
    pending_reply: deque[Loader] = field(default_factory=deque)


class CheckpointStore:
    def __init__(self, root: Path, bare_post_id: str, sort: str):
        root.mkdir(parents=True, exist_ok=True)
        safe = f"{bare_post_id}_{sort}"
        self.comments_path = root / f"{safe}.comments.jsonl"
        self.state_path = root / f"{safe}.state.json"

    def load_comments(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        if not self.comments_path.exists():
            return out
        for line in self.comments_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            cid = str(row.get("comment_id") or "")
            if cid:
                out[cid] = row
        return out

    def append_comments(self, rows: list[dict[str, Any]]):
        if not rows:
            return
        with self.comments_path.open("a", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def load_state(self) -> ResumeState:
        if not self.state_path.exists():
            return ResumeState()
        try:
            obj = json.loads(self.state_path.read_text(encoding="utf-8"))
            return ResumeState(
                completed=bool(obj.get("completed")),
                processed_loader_keys=set(obj.get("processed_loader_keys") or []),
                pending_top=deque(Loader.from_dict(x) for x in (obj.get("pending_top") or [])),
                pending_reply=deque(Loader.from_dict(x) for x in (obj.get("pending_reply") or [])),
            )
        except Exception:
            return ResumeState()

    def save_state(self, state: ResumeState):
        obj = {
            "completed": state.completed,
            "processed_loader_keys": sorted(state.processed_loader_keys),
            "pending_top": [x.to_dict() for x in state.pending_top],
            "pending_reply": [x.to_dict() for x in state.pending_reply],
        }
        tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)
