from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    from app.core.checkpointing import FileCheckpointSaver
except ModuleNotFoundError:  # pragma: no cover - depends on local runtime
    FileCheckpointSaver = None


class CheckpointingTests(unittest.TestCase):
    def test_file_checkpoint_saver_creates_persistent_files(self) -> None:
        if FileCheckpointSaver is None:
            self.skipTest("langgraph is not installed in the current python runtime")
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "checkpoints"
            saver = FileCheckpointSaver(root)

            self.assertTrue((root / "storage.pkl").exists())
            self.assertTrue((root / "writes.pkl").exists())
            self.assertTrue((root / "blobs.pkl").exists())
            saver._sync_all()


if __name__ == "__main__":
    unittest.main()
