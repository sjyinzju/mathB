from __future__ import annotations

import os
import shutil
import time
import uuid
from pathlib import Path


def atomic_promote_q2_run(run_dir: Path, best_dir: Path) -> None:
    """Atomically replace a Q2 artifact directory with one complete run."""
    if run_dir.resolve() == best_dir.resolve():
        raise ValueError("run directory and best directory must differ")
    token = uuid.uuid4().hex
    staged = best_dir.parent / f".{best_dir.name}.staged-{token}"
    backup = best_dir.parent / f".{best_dir.name}.backup-{token}"
    shutil.copytree(run_dir, staged)

    def replace_with_retry(source: Path, destination: Path) -> None:
        last_error: PermissionError | None = None
        for attempt in range(6):
            try:
                os.replace(source, destination)
                return
            except PermissionError as error:
                last_error = error
                time.sleep(0.1 * (attempt + 1))
        assert last_error is not None
        raise last_error

    try:
        if best_dir.exists():
            replace_with_retry(best_dir, backup)
        replace_with_retry(staged, best_dir)
    except Exception:
        if not best_dir.exists() and backup.exists():
            replace_with_retry(backup, best_dir)
        raise
    finally:
        if staged.exists():
            shutil.rmtree(staged)
        if backup.exists():
            shutil.rmtree(backup)
