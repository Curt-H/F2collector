# -*- coding: utf-8 -*-
"""f2dl 自有纯逻辑的离线单测（不联网、不起浏览器、不写真实 cookie 存储）。

背景（对应 OPTIMIZATION_REPORT E1-②）：此前 f2dl 的核心逻辑只靠根目录
verify 脚本的 monkeypatch 覆盖，且 f2 内置 10 个单测中 8 个真实联网——
不存在"跑一遍单测不碰抖音"的选项。本文件补齐离线基线；f2 端的联网测试
维持原样（须显式单独运行）。

运行（项目运行时）：
    python -m pytest f2dl/test/test_f2dl_offline.py -q
"""

import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[2]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from f2dl import apps, confgen, core  # noqa: E402
from f2dl import cookie as cookie_mod  # noqa: E402


# ── core.validate_interval ──────────────────────────────────────────

def test_validate_interval_ok():
    assert core.validate_interval("all") == "all"
    assert core.validate_interval("2024-01-01|2024-12-31") == "2024-01-01|2024-12-31"
    assert core.validate_interval(" 2024-01-01|2024-12-31 ") == "2024-01-01|2024-12-31"
    assert core.validate_interval("") == "all"
    assert core.validate_interval(None) == "all"


def test_validate_interval_rejects_bad_shape():
    with pytest.raises(ValueError):
        core.validate_interval("2024-01-01")  # 缺另一半区间
    with pytest.raises(ValueError):
        core.validate_interval("not-a-date")


# ── core._resolve_incremental（增量窗口语义）─────────────────────────

def test_resolve_incremental_valid():
    last_run, window = core._resolve_incremental(
        "douyin", {"last_run": "2026-09-15 13:54:54"}
    )
    assert last_run == "2026-09-15 13:54:54"
    assert window == f"2026-09-15|{datetime.now():%Y-%m-%d}"


def test_resolve_incremental_degrades_to_full():
    # last_run 缺失/非法 → 降级全量（窗口为空）
    assert core._resolve_incremental("douyin", {}) == ("", "")
    assert core._resolve_incremental("douyin", None) == ("", "")
    assert core._resolve_incremental("douyin", {"last_run": "not-a-date"}) == ("", "")
    assert core._resolve_incremental("douyin", {"last_run": "2026-13-99 99:99:99"}) == ("", "")


# ── core._batch_get_cookie（O2 批次 cookie 缓存）─────────────────────

def test_batch_cookie_cache_and_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_refresh(pk, browser):
        calls["n"] += 1
        return f"cookie_{calls['n']}"

    monkeypatch.setattr(core, "refresh_cookie", fake_refresh)
    core._BATCH_COOKIE_CACHE.clear()
    try:
        c1 = core._batch_get_cookie("douyin", "firefox")
        c2 = core._batch_get_cookie("douyin", "firefox")
        assert c1 == c2 == "cookie_1"
        assert calls["n"] == 1  # TTL 内命中缓存，只抓一次

        core._BATCH_COOKIE_CACHE["douyin"] = (
            "old",
            time.time() - core._BATCH_COOKIE_TTL_SEC - 1,
        )
        c3 = core._batch_get_cookie("douyin", "firefox")
        assert c3 == "cookie_2"  # 超龄自动重抓
        assert calls["n"] == 2
    finally:
        core._BATCH_COOKIE_CACHE.clear()


# ── apps：链接提取 / 名称清洗 / 模式表 ───────────────────────────────

def test_extract_identifier():
    sec = "MS4wLjABAAAA" + "x" * 20
    assert apps.extract_identifier("douyin", f"https://www.douyin.com/user/{sec}?x=1") == sec
    assert (
        apps.extract_identifier("douyin", "https://www.douyin.com/video/7298145681699622182")
        == "7298145681699622182"
    )
    assert apps.extract_identifier("douyin", "https://example.com/nothing") is None
    assert apps.extract_identifier("douyin", "") is None


def test_sanitize_name():
    assert apps.sanitize_name("小水") == "小水"
    assert apps.sanitize_name('a/b:c*d?"<>|') == "a_b_c_d"
    assert apps.sanitize_name("  CON ") == "_CON"  # Windows 保留名加前缀
    assert apps.sanitize_name("...x..") == "x"
    with pytest.raises(ValueError):
        apps.sanitize_name("   ")


def test_interval_modes_cover_modes():
    assert "post" in apps.INTERVAL_MODES
    assert "one" not in apps.INTERVAL_MODES  # one/live 不依赖日期区间
    assert "live" not in apps.INTERVAL_MODES
    assert set(apps.INTERVAL_MODES) <= set(apps.DOUYIN_MODES)


# ── confgen.build_conf（official_defaults 打桩，不跑 f2 子进程）──────

def test_build_conf_fills_fields_and_defaults(monkeypatch):
    monkeypatch.setattr(
        confgen,
        "official_defaults",
        lambda pk: {"cookie": "", "interval": "", "path": None, "naming": ""},
    )
    conf = confgen.build_conf(
        "douyin",
        url="https://www.douyin.com/user/abc",
        mode="post",
        cookie="ck",
        interval="2024-01-01|2024-12-31",
    )
    sec = conf["douyin"]
    assert sec["url"] == "https://www.douyin.com/user/abc"
    assert sec["mode"] == "post"
    assert sec["cookie"] == "ck"
    assert sec["interval"] == "2024-01-01|2024-12-31"  # INTERVAL_MODES 尊重显式区间
    assert sec["naming"] == "{create}_{aweme_id}"  # 高级默认回填（空值才填）
    assert sec["path"] == str(apps.PLATFORMS["douyin"]["download_dir"])


def test_build_conf_one_mode_forces_all(monkeypatch):
    monkeypatch.setattr(confgen, "official_defaults", lambda pk: {})
    conf = confgen.build_conf(
        "douyin", url="u", mode="one", cookie="ck", interval="2024-01-01|2024-12-31"
    )
    assert conf["douyin"]["interval"] == "all"  # one/live 固定 all（f2 语义）


# ── cookie：脱敏展示与粘贴校验（不触碰真实 cookie 存储）──────────────

def test_cookie_mask():
    assert cookie_mod.mask("") == "(空)"
    assert cookie_mod.mask("short") == "short"
    long_cookie = "a" * 16 + "b" * 8 + "X" * 10
    masked = cookie_mod.mask(long_cookie)
    assert masked.startswith("a" * 16 + "...")
    assert masked.endswith("..." + "X" * 8)  # tail 取的是末尾 8 位


def test_paste_cookie_rejects_before_write():
    # 非法输入在 save_cookie 之前即抛错——不会触碰真实 cookie 存储
    with pytest.raises(ValueError):
        cookie_mod.paste_cookie("douyin", "too-short")
    with pytest.raises(ValueError):
        cookie_mod.paste_cookie("douyin", "no-semicolon-value-but-long-enough")
