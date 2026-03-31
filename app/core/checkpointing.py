from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from threading import RLock
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver, PersistentDict


class FileCheckpointSaver(InMemorySaver):
    def __init__(self, root: Path):
        super().__init__()
        self.stack.close()
        self._sync_lock = RLock()
        root.mkdir(parents=True, exist_ok=True)
        self.storage = PersistentDict(lambda: defaultdict(dict), filename=str(root / "storage.pkl"))
        self.writes = PersistentDict(dict, filename=str(root / "writes.pkl"))
        self.blobs = PersistentDict(filename=str(root / "blobs.pkl"))
        for item in (self.storage, self.writes, self.blobs):
            if Path(item.filename).exists():
                item.load()
        self.stack.enter_context(self.storage)
        self.stack.enter_context(self.writes)
        self.stack.enter_context(self.blobs)

    def _sync_all(self) -> None:
        with self._sync_lock:
            for item in (self.storage, self.writes, self.blobs):
                try:
                    item.sync()
                except FileNotFoundError:
                    # PersistentDict uses temp-file swaps; concurrent sync attempts can
                    # race on the same temp path. The next successful sync will persist
                    # the same in-memory state.
                    continue

    def put(self, config, checkpoint, metadata, new_versions):
        result = super().put(config, checkpoint, metadata, new_versions)
        self._sync_all()
        return result

    def put_writes(self, config, writes, task_id, task_path=""):
        result = super().put_writes(config, writes, task_id, task_path)
        self._sync_all()
        return result

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        self._sync_all()

    def delete_for_runs(self, *args: Any, **kwargs: Any) -> None:
        super().delete_for_runs(*args, **kwargs)
        self._sync_all()

    def prune(self, *args: Any, **kwargs: Any) -> None:
        super().prune(*args, **kwargs)
        self._sync_all()
