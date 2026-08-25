from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO


class MarketMakingProcessManager:
    """Supervises the sibling shadow-only process without importing its trading code."""

    def __init__(self, project_root: str | Path, *, auto_start: bool = True) -> None:
        self.project_root = Path(project_root)
        self.executable = self.project_root / ".venv" / "bin" / "market-making-lighter"
        self.log_path = self.project_root / "data" / "market_maker.log"
        self.pid_path = self.project_root / "data" / "market_maker.pid"
        self.auto_start = auto_start
        self._process: asyncio.subprocess.Process | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._log_file: BinaryIO | None = None
        self._lock = asyncio.Lock()
        self.started_at: str | None = None
        self.last_exit_code: int | None = None
        self.last_error: str | None = None

    async def start(self) -> dict[str, object]:
        async with self._lock:
            pid = self._running_pid()
            if pid is not None:
                return self.status()
            if not self.executable.exists():
                self.last_error = f"Executable not found: {self.executable}"
                raise FileNotFoundError(self.last_error)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = self.log_path.open("ab", buffering=0)
            environment = dict(os.environ)
            environment["MM_MODE"] = "shadow"
            self._process = await asyncio.create_subprocess_exec(
                str(self.executable),
                "run",
                cwd=str(self.project_root),
                env=environment,
                stdout=self._log_file,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            self.pid_path.write_text(str(self._process.pid))
            self.started_at = datetime.now(UTC).isoformat()
            self.last_exit_code = None
            self.last_error = None
            self._watch_task = asyncio.create_task(self._watch(self._process), name="market-maker-watch")
        await asyncio.sleep(0.15)
        if self._process and self._process.returncode is not None:
            raise RuntimeError(self.last_error or f"Market maker exited with {self._process.returncode}")
        return self.status()

    async def stop(self, timeout_seconds: float = 10.0) -> dict[str, object]:
        async with self._lock:
            pid = self._running_pid()
            if pid is None:
                self._clear_pid_file()
                return self.status()
            os.kill(pid, signal.SIGINT)
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while self._pid_is_running(pid) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)
        if self._pid_is_running(pid):
            os.kill(pid, signal.SIGKILL)
        if self._process is not None:
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
        self._clear_pid_file()
        return self.status()

    async def shutdown(self) -> None:
        await self.stop()

    def status(self) -> dict[str, object]:
        pid = self._running_pid()
        return {
            "managed": True,
            "running": pid is not None,
            "pid": pid,
            "auto_start": self.auto_start,
            "started_at": self.started_at,
            "last_exit_code": self.last_exit_code,
            "last_error": self.last_error,
            "log_path": str(self.log_path),
        }

    async def _watch(self, process: asyncio.subprocess.Process) -> None:
        return_code = await process.wait()
        self.last_exit_code = return_code
        if return_code != 0:
            self.last_error = f"Market maker exited with code {return_code}"
        if self._process is process:
            self._clear_pid_file()
            self._close_log()

    def _running_pid(self) -> int | None:
        if self._process is not None and self._process.returncode is None:
            return self._process.pid
        try:
            pid = int(self.pid_path.read_text().strip())
        except (FileNotFoundError, ValueError, OSError):
            return None
        return pid if self._pid_is_running(pid) else None

    @staticmethod
    def _pid_is_running(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _clear_pid_file(self) -> None:
        try:
            self.pid_path.unlink()
        except FileNotFoundError:
            pass

    def _close_log(self) -> None:
        if self._log_file is not None:
            self._log_file.close()
            self._log_file = None
