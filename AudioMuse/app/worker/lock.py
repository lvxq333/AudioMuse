"""数据目录独占进程锁（单机单进程边界）。

锁的粒度是「进程 × 数据目录」：服务启动时获取并持有到进程退出，
防止第二个服务进程在同一数据目录再启动消费者，从而保证
“全局最多 3 个并发处理”只在唯一进程内成立。

- 基于 fcntl.flock（仅类 Unix：macOS/Linux）；
- 锁文件一经创建不删除、不重建（避免不同进程锁到不同 inode 造成
  各锁各的假象）；
- 非阻塞尝试：拿不到锁立即抛 DataDirLockError，由调用方决定
  “启动失败”而非降级运行。
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from typing import Optional


class DataDirLockError(RuntimeError):
    """数据目录已被其他进程独占。"""


class DataDirLock:
    """对 data_dir/.audiomuse.lock 的文件锁；支持上下文管理器。"""

    _LOCK_NAME = ".audiomuse.lock"

    def __init__(self, data_dir: Path) -> None:
        self._path = Path(data_dir) / self._LOCK_NAME
        self._fd: Optional[int] = None

    def acquire(self) -> None:
        """非阻塞获取独占锁；已持有时抛 RuntimeError，被占用抛 DataDirLockError。"""
        if self._fd is not None:
            raise RuntimeError("DataDirLock 已持有，不能重复 acquire")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise DataDirLockError(
                    f"数据目录已被其他进程占用: {self._path}"
                ) from exc
            raise
        self._fd = fd

    def release(self) -> None:
        """释放锁并关闭 fd；未持有则空操作。"""
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "DataDirLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()
