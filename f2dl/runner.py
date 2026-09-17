"""f2 CLI 子进程封装：统一 utf-8 环境、工具根为 CWD、可自动应答确认提示。"""

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from .apps import TOOL_ROOT

#: init-config / auto-cookie 等短命令的超时上限（含 f2 启动时的联网版本检查）
SHORT_TIMEOUT = 180


def _f2_command() -> List[str]:
    """构造调用 f2 的命令前缀。

    f2 已随仓库 vendor（顶层 f2/ 包，含本地补丁），默认直接用当前解释器以
    `python -m f2` 方式调用——与 pip 安装状态、.exe 启动器、目录搬迁完全解耦。
    如需改用其他 f2（例如全局安装的新版本），设置环境变量 F2DL_F2_EXE
    为其可执行文件完整路径即可覆盖。
    """
    env_exe = os.environ.get("F2DL_F2_EXE", "").strip()
    if env_exe:
        path = Path(env_exe)
        if path.is_file():
            return [str(path)]
        raise FileNotFoundError(f"环境变量 F2DL_F2_EXE 指定的路径不存在: {env_exe}")
    return [sys.executable, "-m", "f2"]


def run_f2(args: List[str], feed_yes: bool = False, timeout: Optional[int] = None) -> int:
    """运行 f2 子命令，返回退出码。

    - 强制 PYTHONIOENCODING=utf-8：GBK 控制台下 f2 的 rich 输出（emoji）会直接崩溃。
    - 强制 PYTHONUNBUFFERED=1（Fix 2, 2026-09-16）：子进程 stdout 接管道后
      Python 会从行缓冲切到 8KB 块缓冲，日志会积压到攒满才刷出。
    - CWD 固定为工具根目录，f2 的 logs/ 与相对路径行为可预期。
    - feed_yes=True 时向 stdin 喂 "y\\n"，用于自动通过 f2 内部的 click.confirm 确认提示。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [*_f2_command(), *args]
    completed = subprocess.run(
        cmd,
        cwd=str(TOOL_ROOT),
        env=env,
        input="y\n" if feed_yes else None,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )
    return completed.returncode


def run_f2_captured(args: List[str], feed_yes: bool = False) -> "tuple[int, str]":
    """运行 f2 子命令：输出实时转发到控制台的同时捕获全文，返回 (退出码, 输出)。

    供编排层做 403 风控特征检测与 risk_stats 统计。注意：输出经管道后
    f2 的 rich 进度条会退化为普通日志行（rich 检测到非 tty），属预期行为。

    本地补丁(Fix 1, 2026-09-16)：转发循环改用 read1——管道上的 read(4096)
    会阻塞到攒满 4096 字节才返回，日志被二次积压成"一股脑输出"；read1
    只返回当前已到达的数据，到多少转多少。配合子进程侧 PYTHONUNBUFFERED
    （Fix 2）实现端到端实时。
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    cmd = [*_f2_command(), *args]
    with subprocess.Popen(
        cmd,
        cwd=str(TOOL_ROOT),
        env=env,
        stdin=subprocess.PIPE if feed_yes else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    ) as proc:
        if feed_yes and proc.stdin is not None:
            try:
                proc.stdin.write(b"y\n")
                proc.stdin.flush()
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        chunks = []
        stream = proc.stdout
        while stream is not None:
            chunk = stream.read1(4096)
            if not chunk:
                break
            chunks.append(chunk)
            try:
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
            except OSError:  # noqa: BLE001 - 控制台不可写时只捕获不转发
                pass
        rc = proc.wait()
    return rc, b"".join(chunks).decode("utf-8", errors="replace")
