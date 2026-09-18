"""f2dl —— 基于 f2 CLI 的某平台/微博配置生成与下载小工具。

约定（与设计决策对应）：
- 无参数运行进入交互向导（Q1）；带参数时一条命令直接生成配置并下载。
- 配置生成以 f2 官方默认（--init-config）为底稿，程序改写关键字段（Q3）。
- cookie 独立存放于 dy/cookie.yaml、wb/cookie.yaml，抓一次长期复用（Q11/Q16）。
- 任务配置按作者/别名持久化到 dy/conf、wb/conf，重跑即追更（Q6）。
"""

from rich.console import Console

__version__ = "0.1.0"

#: 全局共享控制台。注意：目标是 Windows GBK 控制台，输出内容不要包含 emoji。
console = Console()


def setup_streams() -> None:
    """仅在 stdout 被重定向/管道时强制 UTF-8（与 f2 子进程行为一致）。

    交互式控制台保持默认：rich 在真实控制台上走 Win32 Unicode API，
    GBK 控制台也不会乱码；click 的输入回显同样不受影响。
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        if stream is not None and not stream.isatty() and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:  # noqa: BLE001 - 编码重配置失败不阻塞主流程
                pass

