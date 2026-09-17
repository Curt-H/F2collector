# ruff: noqa
"""端到端验证:DouyinHandler.fetch_one_video 三通道(Feed → SSR → Web)。"""
import asyncio
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from f2.apps.douyin.handler import DouyinHandler  # noqa: E402

AWEME_ID = "7674503540953550922"

KWARGS = {
    "cookie": "",
    "headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"},
    "proxies": {"http://": None, "https://": None},
    "path": r"D:\DEV\F2test\Download\dy_verify",
    "naming": "{aweme_id}",
    "mode": "one",
    "max_retries": 1,
    "timeout": 15,
    "auto_cookie": False,
    "bark": "",
}


async def main():
    handler = DouyinHandler(KWARGS)
    video = await handler.fetch_one_video(AWEME_ID)
    print("--- fetch_one_video OK ---")
    print(f"  aweme_id : {video.aweme_id}")
    print(f"  nickname : {video.nickname}")
    print(f"  desc     : {str(video.desc)[:30]!r}")
    addr = video.video_play_addr
    print(f"  play_addr: {type(addr).__name__}" + (f" len={len(addr)}" if isinstance(addr, list) else f" {str(addr)[:50]}"))


if __name__ == "__main__":
    asyncio.run(main())
