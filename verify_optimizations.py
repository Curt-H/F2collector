# ruff: noqa
"""O1/O2/O3/W1 优化项综合验证（一次性脚本）。"""
import asyncio
import json
import time
import types
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys_path_added = False
import sys

if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))
    sys_path_added = True

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SEC = "MS4wLjABAAAAZgFvH5X9vW0JfoAZWxoRPMMwStbsk5hK3AplFyKTfIg"
ok = True


def report(name, good, detail=""):
    global ok
    print(f"[{name}] {'✅' if good else '❌'} {detail}")
    ok = ok and good


# ── O2：批次 cookie 缓存（纯逻辑模拟） ──
from f2dl import core  # noqa: E402

_calls = {"n": 0}


def _fake_refresh(pk, browser):
    _calls["n"] += 1
    return f"fake_cookie_{_calls['n']}"


core.refresh_cookie = _fake_refresh
c1 = core._batch_get_cookie("douyin", "firefox")
c2 = core._batch_get_cookie("douyin", "firefox")
report(
    "O2_cache_hit",
    c1 == c2 and _calls["n"] == 1,
    f"两次调用一次抓取(cookie={c1})",
)
core._BATCH_COOKIE_CACHE["douyin"] = ("old", time.time() - 1801)
c3 = core._batch_get_cookie("douyin", "firefox")
report("O2_ttl_regrab", _calls["n"] == 2 and c3 == "fake_cookie_2", "超龄自动重抓")
core._BATCH_COOKIE_CACHE.clear()

# ── O3：列表数据建目录/写库 ──
from f2.apps.douyin.db import AsyncUserDB  # noqa: E402
from f2.apps.douyin.filter import UserPostFilter  # noqa: E402
from f2.apps.douyin.handler import DouyinHandler  # noqa: E402


async def _o3():
    sample = TOOL_ROOT / "logs" / "probe_browser_post_sample.json"
    if not sample.exists():
        print("[O3_list_folder] ⚠ 样本缺失,跳过")
        return True
    data = json.loads(sample.read_text(encoding="utf-8"))
    video = UserPostFilter(data)
    kw = {
        "path": str(TOOL_ROOT / "logs" / "test_o3_dir"),
        "folderize": False,
        "naming": "{aweme_id}",
    }
    db_file = TOOL_ROOT / "logs" / "test_o3_users.db"
    if db_file.exists():
        db_file.unlink()
    async with AsyncUserDB(str(db_file)) as db:
        path = await DouyinHandler.get_or_add_user_data_from_list(
            types.SimpleNamespace(), kw, SEC, db, video
        )
        info = await db.get_user_info(SEC)
        good = (
            path.exists()
            and bool(info.get("nickname"))
            and "Softutu" in str(info.get("nickname"))
        )
        print(
            f"[O3_list_folder] 目录={path.name}, DB昵称={info.get('nickname')}"
        )
        return good


o3_good = asyncio.run(_o3())
report("O3_list_folder", o3_good, "列表数据建目录+写库")

# ── O1：进程内真实抓取（需要 Firefox 已关闭；失败仅告警不判负） ──
try:
    from f2dl.cookie import grab_cookie, clear_cookie  # noqa: E402

    t0 = time.time()
    ok1, cookie = grab_cookie("douyin")
    dt = time.time() - t0
    if ok1:
        report(
            "O1_grab_live",
            len(cookie) > 100,
            f"进程内抓取 {len(cookie)} 字节 / {dt:.1f}s（含读取库）",
        )
    else:
        print(f"[O1_grab_live] ⚠ 抓到空 cookie（浏览器未登录？），跳过判定")
    clear_cookie("douyin")
except Exception as exc:  # noqa: BLE001
    print(f"[O1_grab_live] ⚠ 环境不允许（Firefox 开着？）：{exc}，跳过判定")

# ── W1：常驻实例复用（两次真实会话，第二次应免 launch） ──
from probe_browser_post import load_cookie  # noqa: E402
from f2.apps.douyin import browser_fetch as b  # noqa: E402


async def _w1():
    cookie = load_cookie()
    future_ms = int(time.time() * 1000) + 365 * 24 * 3600 * 1000
    t0 = time.time()
    r1 = await asyncio.to_thread(
        b._collect_pages_sync, cookie, SEC, 0, future_ms, True, 0.0
    )
    t1 = time.time()
    r2 = await asyncio.to_thread(
        b._collect_pages_sync, cookie, SEC, 0, future_ms, True, 0.0
    )
    t2 = time.time()
    holder = b._BROWSER_HOLDER.get("headless")
    connected = bool(holder and holder["browser"].is_connected())
    print(
        f"[W1_reuse] 会话1 {t1 - t0:.1f}s(含launch), "
        f"会话2 {t2 - t1:.1f}s(免launch), "
        f"实例连接保持={connected}"
    )
    b._shutdown_browsers()
    return (
        r1["end_reason"] == "min_cursor"
        and r2["end_reason"] == "min_cursor"
        and connected
    )


w1_good = asyncio.run(_w1())
report("W1_reuse", w1_good, "常驻实例跨会话复用")

print("=" * 60)
print("全部通过 ✅" if ok else "存在失败 ❌")
sys.exit(0 if ok else 1)
