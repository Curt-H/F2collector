"""f2 CLI 子进程封装：统一 utf-8 环境、工具根为 CWD、可自动应答确认提示。"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import List, Optional

from . import console
from .apps import TOOL_ROOT

#: init-config / auto-cookie 等短命令的超时上限（含 f2 启动时的联网版本检查）
SHORT_TIMEOUT = 180

#: 输出空闲看门狗（ZOMBIE-1，2026-11-09）：子进程连续这么久零输出且未退出，
#: 或输出关闭后这么久仍未退出，即判挂死强制结束。取 30 分钟而非更短：管道
#: 模式下 f2 的 rich 进度条退化为纯日志，大文件下载可能长时间静默；30 分钟
#: 对齐引擎侧滚动总预算的量级，误杀可由任务级重试 + .tmp 断点续传自愈。
#: 背景：2026-11 事故复盘——11/3 挂死的批次进程因无限等待存活 6 天成僵尸，
#: 最终死于系统页面故障（0xC0000006）并连带当天批量进程异常退出。
CAPTURE_IDLE_TIMEOUT_SEC = 30 * 60


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

    本地补丁(ZOMBIE-1, 2026-11-09)：阻塞读交给后台泵线程，主线程负责转发
    与空闲看门狗——子进程挂死（连续 30 分钟零输出且未退出，见
    CAPTURE_IDLE_TIMEOUT_SEC）时强制 kill 并按失败返回，编排进程不再无限
    等待成僵尸。
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
        lock = threading.Lock()

        def _pump() -> None:
            # 阻塞读（read1 语义见 Fix 1 注释）放后台线程，主线程才能做看门狗。
            stream = proc.stdout
            while stream is not None:
                try:
                    chunk = stream.read1(4096)
                except (OSError, ValueError):  # 管道关闭/中断
                    return
                if not chunk:
                    return  # EOF：子进程关闭了输出
                with lock:
                    chunks.append(chunk)

        pump = threading.Thread(target=_pump, name="f2-output-pump", daemon=True)
        pump.start()

        def _forward(fresh: bytes) -> None:
            if not fresh:
                return
            try:
                sys.stdout.buffer.write(fresh)
                sys.stdout.buffer.flush()
            except OSError:  # noqa: BLE001 - 控制台不可写时只捕获不转发
                pass

        def _forward_pending(forwarded: int) -> int:
            with lock:
                fresh = b"".join(chunks[forwarded:])
                total = len(chunks)
            _forward(fresh)
            return total

        forwarded = 0
        last_output = time.monotonic()
        idle_killed = False
        while pump.is_alive():
            time.sleep(0.5)
            total = _forward_pending(forwarded)
            if total > forwarded:
                forwarded = total
                last_output = time.monotonic()
            elif time.monotonic() - last_output > CAPTURE_IDLE_TIMEOUT_SEC:
                idle_killed = True
                try:
                    proc.kill()
                except OSError:
                    pass
                break
        pump.join(timeout=5)
        _forward_pending(forwarded)
        try:
            rc = proc.wait(timeout=CAPTURE_IDLE_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            # 输出已关（EOF）却迟迟不退出——同样按挂死处理
            idle_killed = True
            try:
                proc.kill()
            except OSError:
                pass
            rc = proc.wait()
        if idle_killed:
            console.print(
                f"[yellow]子进程连续 {CAPTURE_IDLE_TIMEOUT_SEC // 60} 分钟零输出，"
                "已判挂死并强制结束（ZOMBIE-1 看门狗）；任务按失败处理，"
                "自动重试将从 .tmp 断点续传。[/yellow]"
            )
    return rc, b"".join(chunks).decode("utf-8", errors="replace")
