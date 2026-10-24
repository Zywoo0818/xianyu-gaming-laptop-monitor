"""跨进程扫描锁：禁止手动扫描和定时任务同时运行。"""
from __future__ import annotations

import os
import time
from pathlib import Path


class ScanBusy(Exception):
    """已有另一轮扫描在运行。"""


class ScanLock:
    """基于系统文件锁的实现，进程退出（包括崩溃）后锁会自动释放。"""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self) -> "ScanLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("r+b") if self.path.exists() else self.path.open("w+b")
        try:
            if self.path.stat().st_size == 0:
                handle.write(b" ")
                handle.flush()
            # 第 0 字节只当锁用：Windows 的字节锁会拦住同区间的读取，
            # 持有者信息从第 1 字节开始写，冲突时才能读到是谁占着锁。
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                handle.seek(1)
                holder = handle.read(200).decode("utf-8", "replace").strip()
            except OSError:
                pass
            handle.close()
            raise ScanBusy(holder or "另一个扫描进程正在运行") from None

        self._handle = handle
        handle.seek(0)
        handle.truncate(1)
        handle.seek(1)
        handle.write(
            f"pid={os.getpid()} started={time.strftime('%Y-%m-%d %H:%M:%S')}\n".encode("utf-8")
        )
        handle.flush()
        return self

    def __exit__(self, *exc: object) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()
