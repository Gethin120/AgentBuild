from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, List


def _parse_env_line(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    key, value = text.split("=", 1)
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        value = value[1:-1]
    return key, value


def load_dotenv_files(paths: Iterable[Path], *, override: bool = False) -> Dict[str, str]:
    loaded: Dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(raw_line)
            if not parsed:
                continue
            key, value = parsed
            if override or key not in os.environ:
                os.environ[key] = value
            loaded[key] = os.environ.get(key, value)
    return loaded


def load_project_env(root: Path, *, override: bool = False) -> Dict[str, str]:
    return load_dotenv_files(
        [
            root / ".env",
            root / ".env.local",
        ],
        override=override,
    )


def preferred_python_command(root: Path) -> List[str]:
    conda_bin = Path("/opt/miniconda3/bin/conda")
    if conda_bin.exists():
        return [str(conda_bin), "run", "-n", "llm_local", "python"]
    return [os.environ.get("PYTHON", "python3")]
