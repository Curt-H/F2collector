"""F2 下载工具入口。

用法：
    python app.py                                  # 交互向导
    python app.py -p douyin -u <链接> -M post      # 直接生成配置并下载
    python app.py -p douyin -u <链接> -M post --gen-only   # 只生成配置
    python app.py cookie -p douyin                 # cookie 抓取/粘贴
"""

import sys
import traceback
from datetime import datetime
from pathlib import Path

from f2dl import setup_streams

setup_streams()


def _install_crash_hook() -> None:
    """顶层崩溃保险（CRASH-2，2026-11-09）：未捕获异常先落盘、后打印。

    事故背景：2026-11-09 11:42 批量追更整批崩溃，用户终端只截到第一帧
    （app.py:17），完整 traceback 事后无法找回，根因排查走了大弯路。
    本 hook 在交给默认打印之前，把完整 traceback 写入 logs/last_crash.txt
    ——只要磁盘还能写，全尸一定留底；落盘失败不影响默认打印。
    """
    tool_root = Path(__file__).resolve().parent
    prev_hook = sys.excepthook

    def _hook(exc_type, exc, tb):
        try:
            crash_file = tool_root / "logs" / "last_crash.txt"
            crash_file.parent.mkdir(parents=True, exist_ok=True)
            crash_file.write_text(
                f"time={datetime.now():%Y-%m-%d %H:%M:%S}\n"
                + "".join(traceback.format_exception(exc_type, exc, tb)),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 - 落盘失败不能阻止默认打印
            pass
        prev_hook(exc_type, exc, tb)

    sys.excepthook = _hook


_install_crash_hook()

from f2dl.cli import main  # noqa: E402

if __name__ == "__main__":
    main()

