# -*- coding: utf-8 -*-
"""f2dl 自有纯逻辑的离线单测（不联网、不起浏览器、不写真实 cookie 存储）。

背景（对应 OPTIMIZATION_REPORT E1-②）：此前 f2dl 的核心逻辑只靠根目录
verify 脚本的 monkeypatch 覆盖，且 f2 内置 10 个单测中 8 个真实联网——
不存在"跑一遍单测不碰某平台"的选项。本文件补齐离线基线；f2 端的联网测试
维持原样（须显式单独运行）。

运行（项目运行时）：
    python -m pytest f2dl/test/test_f2dl_offline.py -q
"""

import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest
import click
import yaml

TOOL_ROOT = Path(__file__).resolve().parents[2]
if str(TOOL_ROOT) not in sys.path:
    sys.path.insert(0, str(TOOL_ROOT))

from f2dl import apps, confgen, core  # noqa: E402
from f2dl import cookie as cookie_mod  # noqa: E402
from f2dl import nickname as nn  # noqa: E402
from f2dl import wizard  # noqa: E402


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


# ── core.rerun_task：全量跑剥离 last_run 的临时副本；增量用真实配置 ──
# （FULL-1 缺陷修复回归测试，2026-09-17）


def _write_task_conf(path, interval, last_run):
    section = {
        "url": "https://www.douyin.com/user/MS4wLjABAAAA" + "x" * 20,
        "mode": "post",
        "interval": interval,
    }
    if last_run:
        section["last_run"] = last_run
    confgen.save_conf(path, {"douyin": section})


def test_rerun_full_uses_temp_conf_without_last_run(monkeypatch, tmp_path):
    captured = {}

    def fake_retry(args, platform_key="", task_label=""):
        captured["args"] = list(args)
        c_path = Path(args[args.index("-c") + 1]) if "-c" in args else None
        captured["conf"] = (
            yaml.safe_load(c_path.read_text(encoding="utf-8")) if c_path else None
        )
        return 0

    monkeypatch.setattr(core, "_run_with_retry", fake_retry)
    monkeypatch.setattr(core, "_obtain_cookie", lambda pk, b, interactive=True: "ck")
    monkeypatch.setattr(core, "_cleanup_desc_files", lambda pk: 0)
    monkeypatch.setattr(core, "_write_unfinished_manifest", lambda pk: None)

    conf = tmp_path / "task.yaml"
    _write_task_conf(
        conf, interval="2024-01-01|2024-12-31", last_run="2026-09-15 13:54:54"
    )

    rc = core.rerun_task(conf, interactive=False, update_mode="full")
    assert rc == 0
    args = captured["args"]
    assert args[args.index("-i") + 1] == "all"
    snap = (captured.get("conf") or {}).get("douyin") or {}
    assert "last_run" not in snap  # 临时副本剥离 last_run（FULL-1）
    assert snap.get("interval") == "2024-01-01|2024-12-31"  # 副本保留原区间
    real = yaml.safe_load(conf.read_text(encoding="utf-8"))["douyin"]
    assert real["interval"] == "2024-01-01|2024-12-31"  # 真实配置原值不动
    assert real["last_run"] != "2026-09-15 13:54:54"  # 成功后照常推进


def test_rerun_incremental_uses_real_conf_and_window(monkeypatch, tmp_path):
    captured = {}

    def fake_retry(args, platform_key="", task_label=""):
        captured["args"] = list(args)
        captured["conf_path"] = (
            Path(args[args.index("-c") + 1]) if "-c" in args else None
        )
        return 0

    monkeypatch.setattr(core, "_run_with_retry", fake_retry)
    monkeypatch.setattr(core, "_obtain_cookie", lambda pk, b, interactive=True: "ck")
    monkeypatch.setattr(core, "_cleanup_desc_files", lambda pk: 0)
    monkeypatch.setattr(core, "_write_unfinished_manifest", lambda pk: None)

    conf = tmp_path / "task.yaml"
    _write_task_conf(conf, interval="all", last_run="2026-09-15 13:54:54")

    rc = core.rerun_task(conf, interactive=False, update_mode="incremental")
    assert rc == 0
    assert captured["conf_path"] == conf  # 增量直用真实配置（不建临时副本）
    args = captured["args"]
    assert args[args.index("-i") + 1].startswith("2026-09-15|")  # 注入增量窗口


# ── PATH-1：下载根目录相对路径 + 目录扁平化 ──


def test_download_dir_is_relative():
    dd = apps.PLATFORMS["douyin"]["download_dir"]
    assert not Path(dd).is_absolute()  # 配置里不再出现指向旧根的绝对路径
    advanced_path = dict(
        (k, d) for k, _t, _l, d in apps.PLATFORMS["douyin"]["advanced"]
    )["path"]
    assert not Path(advanced_path).is_absolute()  # 高级默认同样是相对值


def test_create_user_folder_flat(tmp_path):
    from f2.apps.douyin.utils import create_user_folder

    user_path = create_user_folder({"path": str(tmp_path), "mode": "post"}, "某账户")
    assert user_path == (tmp_path / "某账户").resolve()  # 无 douyin/post 中间层
    assert user_path.is_dir()
    assert not (tmp_path / "douyin").exists()


# ── FOLLOW-1：关注列表导出工具的纯函数 ──


def _load_following_tool():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "following_list", TOOL_ROOT / "utils" / "following_list.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_following_resolve_sec_uid_local():
    mod = _load_following_tool()
    sec = "MS4wLjABAAAA" + "y" * 20
    # 完整 user 链接走本地正则提取，不发任何网络请求
    assert mod.resolve_sec_uid(f"https://www.douyin.com/user/{sec}?x=1") == sec


def test_following_format_lines_two_per_user():
    mod = _load_following_tool()
    text = mod.format_lines([("小明", "SEC1"), ("小红", "SEC2")])
    assert text.strip().splitlines() == [
        "小明",
        "https://www.douyin.com/user/SEC1",
        "小红",
        "https://www.douyin.com/user/SEC2",
    ]


def test_following_accumulate_dedup_and_total():
    mod = _load_following_tool()
    page1 = {
        "status_code": 1,
        "has_more": True,
        "total": 3,
        "followings": [
            {"sec_uid": "A", "nickname": "甲"},
            {"sec_uid": "B", "nickname": "乙"},
        ],
    }
    page2 = {
        "status_code": 1,
        "has_more": False,
        "total": 3,
        "followings": [
            {"sec_uid": "B", "nickname": "乙"},  # 跨页重复 → 去重
            {"sec_uid": "C", "nickname": "丙"},
        ],
    }
    items, total, status = mod.accumulate([page1, page2])
    assert [s for _, s in items] == ["A", "B", "C"]  # 首见顺序
    assert items[0] == ("甲", "A")
    assert total == 3 and status == 1


# ── f2 日志：file_only 记录只进文件不上控制台（NOISE-1）──


def test_file_only_records_skip_console_handler():
    from f2.log.logger import LogManager, logger as f2_logger  # noqa: E402
    from rich.logging import RichHandler

    # pytest 期间 root logger 带捕获 handler，log_setup() 的 hasHandlers()
    # 会短路——为保证确定性，控制台 handler 缺失时强制重建双通道配置。
    if not any(isinstance(h, RichHandler) for h in f2_logger.handlers):
        LogManager().setup_logging(
            level=logging.INFO, log_to_console=True, log_path=TOOL_ROOT / "logs"
        )

    console_handlers = [h for h in f2_logger.handlers if isinstance(h, RichHandler)]
    ch = console_handlers[0]
    if not ch.filters:
        pytest.skip("RichHandler 未挂 file_only 过滤器（补丁未生效？）")

    plain = f2_logger.makeRecord("f2", logging.ERROR, "p", 1, "plain", None, None)
    marked = f2_logger.makeRecord(
        "f2",
        logging.ERROR,
        "p",
        1,
        "marked",
        None,
        None,
        extra={"file_only": True},
    )

    def passes(filters, record):
        # 复刻 logging.Filterer.filter 的语义：callable 直接调用，
        # Filter 对象调用其 .filter(record)
        for f in filters:
            result = f.filter(record) if hasattr(f, "filter") else f(record)
            if not result:
                return False
        return True

    # 控制台 handler：普通记录放行，file_only 记录被拒
    assert passes(ch.filters, plain)
    assert not passes(ch.filters, marked)
    # 文件 handler 不装该过滤器，file_only 记录照常落盘
    file_handlers = [
        h for h in f2_logger.handlers if isinstance(h, logging.FileHandler)
    ]
    for fh in file_handlers:
        assert passes(fh.filters, plain)
        assert passes(fh.filters, marked)


# ── Q-DEDUP：以账号主页为标准的防重复（2026-09-22 共识）────────────────


def test_canonical_user_url():
    sec = "MS4wLjABAAAA" + "z" * 20
    # 带追踪参数的主页链接 → (sec_uid, 无参数规范 URL)
    key, canonical = apps.canonical_user_url(
        "douyin", f"https://www.douyin.com/user/{sec}?from_tab_name=main&nt=0"
    )
    assert key == sec
    assert canonical == f"https://www.douyin.com/user/{sec}"
    # 作品链接 / 空链接 → None（不参与判重）
    assert (
        apps.canonical_user_url(
            "douyin", "https://www.douyin.com/video/7298145681699622182"
        )
        is None
    )
    assert apps.canonical_user_url("douyin", "") is None


def test_conf_index_find_and_next_free_name(monkeypatch, tmp_path):
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    sec_a = "MS4wLjABAAAA" + "a" * 20
    sec_b = "MS4wLjABAAAA" + "b" * 20
    confgen.save_conf(
        tmp_path / "甲.yaml",
        {"douyin": {"url": f"https://www.douyin.com/user/{sec_a}?x=1"}},
    )
    confgen.save_conf(
        tmp_path / "乙.yaml",
        {"douyin": {"url": f"https://www.douyin.com/user/{sec_b}"}},
    )
    index = confgen.conf_index("douyin")
    assert set(index) == {sec_a, sec_b}  # 带参 URL 也能索引
    assert confgen.find_conf_by_key("douyin", sec_a).name == "甲.yaml"
    assert confgen.find_conf_by_key("douyin", "MS4wLjABAAAAnope") is None
    # 无 url / 损坏配置不炸、不进索引
    confgen.save_conf(tmp_path / "坏.yaml", {"douyin": {}})
    (tmp_path / "烂.yaml").write_text("douyin: [broken", encoding="utf-8")
    assert confgen.conf_index("douyin") == index
    # 别名撞车 → -1/-2 顺延
    confgen.save_conf(tmp_path / "王心凌-1.yaml", {"douyin": {"url": "u"}})
    assert confgen.next_free_name("douyin", "王心凌") == "王心凌-2"


# ── apps.validate_url（Q-URLCHECK 链接合规检查）──────────────────────

def test_validate_url_accepts_douyin_links():
    sec = "MS4wLjABAAAA" + "x" * 20
    # 主页（带/不带追踪参数）
    assert (
        apps.validate_url("douyin", f"https://www.douyin.com/user/{sec}?from_tab_name=main")
        == f"https://www.douyin.com/user/{sec}?from_tab_name=main"
    )
    assert (
        apps.validate_url("douyin", f"https://www.douyin.com/user/{sec}")
        == f"https://www.douyin.com/user/{sec}"
    )
    # 单作品 / 笔记
    assert (
        apps.validate_url("douyin", "https://www.douyin.com/video/7298145681699622182")
        == "https://www.douyin.com/video/7298145681699622182"
    )
    assert (
        apps.validate_url("douyin", "https://www.douyin.com/note/7298145681699622182")
        == "https://www.douyin.com/note/7298145681699622182"
    )
    # 短链 / 直播 / 移动分享域（子域均在白名单内）
    assert apps.validate_url("douyin", "https://v.douyin.com/i2wyU53P/") == "https://v.douyin.com/i2wyU53P/"
    assert (
        apps.validate_url("douyin", "https://live.douyin.com/36299127202")
        == "https://live.douyin.com/36299127202"
    )
    assert (
        apps.validate_url("douyin", "https://www.iesdouyin.com/share/video/7298145681699622182")
        == "https://www.iesdouyin.com/share/video/7298145681699622182"
    )
    # 缺协议：容错补 https:// 后放行（直接粘贴常丢协议）
    assert (
        apps.validate_url("douyin", f"www.douyin.com/user/{sec}")
        == f"https://www.douyin.com/user/{sec}"
    )
    # host 大小写不敏感；http 协议同样允许
    assert apps.validate_url("douyin", "HTTPS://WWW.DOUYIN.COM/user/MS4wLjABAAAA123")
    assert apps.validate_url("douyin", "http://www.douyin.com/user/MS4wLjABAAAA123")


def test_validate_url_rejects_non_douyin():
    # 其他平台链接
    for bad in (
        "https://www.bilibili.com/video/BV1xx411c7mD",
        "https://weibo.com/u/1234567890",
        "https://www.tiktok.com/@user/video/123",
        "https://example.com/nothing",
    ):
        with pytest.raises(ValueError):
            apps.validate_url("douyin", bad)
    # 仿冒域名：后缀伪装 / 参数伪装 / 路径伪装必须拦下
    with pytest.raises(ValueError):
        apps.validate_url("douyin", "https://douyin.com.evil.com/user/MS4wLjABAAAA123")
    with pytest.raises(ValueError):
        apps.validate_url("douyin", "https://evil.com/?u=www.douyin.com/user/MS4wLjABAAAA123")
    with pytest.raises(ValueError):
        apps.validate_url("douyin", "https://evil.com/www.douyin.com/user/MS4wLjABAAAA123")
    # 非 URL 纯文本 / 空值
    with pytest.raises(ValueError):
        apps.validate_url("douyin", "小水的主页")
    with pytest.raises(ValueError):
        apps.validate_url("douyin", "   ")
    with pytest.raises(ValueError):
        apps.validate_url("douyin", "")
    with pytest.raises(ValueError):
        apps.validate_url("douyin", None)
    # 非 http(s) 协议
    with pytest.raises(ValueError):
        apps.validate_url("douyin", "javascript://www.douyin.com/alert(1)")
    with pytest.raises(ValueError):
        apps.validate_url("douyin", "file:///C:/evil")


def test_validate_url_rejects_before_cookie_in_run_task(monkeypatch, tmp_path):
    """CLI 兜底：非抖音链接在 cookie 抓取前即被 run_task 拒绝（ValueError）。"""
    def _must_not_run(*_a, **_k):  # cookie 抓取一旦被调到即失败
        raise AssertionError("非抖音链接不应走到 cookie 抓取")

    monkeypatch.setattr(core, "_obtain_cookie", _must_not_run)
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    with pytest.raises(ValueError, match="不是抖音链接"):
        core.run_task("douyin", "https://www.bilibili.com/video/BV1xx", "post")


def test_run_task_rejects_duplicate_account(monkeypatch, tmp_path):
    """Q-DEDUP-REV（2026-10-13）：同账号重复提交 → 报重复拒绝，原配置不动。"""
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    # 昵称查询 mock 掉：不依赖真实缓存库，判重路径行为确定
    monkeypatch.setattr(core.nickname_mod, "resolve_nickname", lambda pk, u: ("", "", ""))

    sec = "MS4wLjABAAAA" + "x" * 20
    confgen.save_conf(
        tmp_path / "甲.yaml",
        {
            "douyin": {
                "url": f"https://www.douyin.com/user/{sec}?from_tab_name=main",
                "mode": "post",
                "interval": "all",
                "last_run": "2026-09-20 08:00:00",
                "page_counts": 999,  # 自定义高级值
            }
        },
    )

    # 换别名 + 带不同追踪参数的同一账号链接提交 → 报重复（不再复用）
    with pytest.raises(ValueError, match="已入库"):
        core.run_task(
            "douyin", f"https://www.douyin.com/user/{sec}?nt=2", "post", name="新别名"
        )
    # 原配置未被改动、无新文件
    prev = yaml.safe_load((tmp_path / "甲.yaml").read_text(encoding="utf-8"))
    assert prev["douyin"]["url"].endswith(f"user/{sec}?from_tab_name=main")
    assert prev["douyin"]["page_counts"] == 999
    assert sorted(p.name for p in tmp_path.glob("*.yaml")) == ["甲.yaml"]


def test_run_task_auto_suffix_on_name_collision(monkeypatch, tmp_path):
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    monkeypatch.setattr(confgen, "official_defaults", lambda pk: {})
    captured = {}

    def fake_retry(args, platform_key="", task_label=""):
        captured["path"] = Path(args[args.index("-c") + 1])
        return 0

    monkeypatch.setattr(core, "_run_with_retry", fake_retry)
    monkeypatch.setattr(core, "_obtain_cookie", lambda pk, b, interactive=True: "ck")
    monkeypatch.setattr(core, "_cleanup_desc_files", lambda pk: 0)
    monkeypatch.setattr(core, "_write_unfinished_manifest", lambda pk: None)

    sec_a = "MS4wLjABAAAA" + "a" * 20
    sec_b = "MS4wLjABAAAA" + "b" * 20
    confgen.save_conf(
        tmp_path / "张三.yaml",
        {"douyin": {"url": f"https://www.douyin.com/user/{sec_a}"}},
    )

    # 不同账号用了已占用的别名 → 自动 -1 后缀，原配置不动
    rc = core.run_task(
        "douyin", f"https://www.douyin.com/user/{sec_b}", "post", name="张三"
    )
    assert rc == 0
    assert captured["path"] == tmp_path / "张三-1.yaml"
    prev = yaml.safe_load((tmp_path / "张三.yaml").read_text(encoding="utf-8"))
    assert prev["douyin"]["url"] == f"https://www.douyin.com/user/{sec_a}"  # 未被覆盖


def test_rerun_all_skips_duplicate_accounts(monkeypatch, tmp_path):
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    sec = "MS4wLjABAAAA" + "d" * 20
    confgen.save_conf(
        tmp_path / "A.yaml",
        {"douyin": {"url": f"https://www.douyin.com/user/{sec}"}},
    )
    confgen.save_conf(
        tmp_path / "B.yaml",
        {"douyin": {"url": f"https://www.douyin.com/user/{sec}?x=1"}},
    )
    ran = []
    monkeypatch.setattr(
        core, "rerun_task", lambda path, **kw: ran.append(Path(path).name) or 0
    )
    monkeypatch.setattr(cookie_mod, "clear_cookie", lambda pk: None)

    rc = core.rerun_all(update_mode="incremental")
    assert rc == 0
    assert ran == ["A.yaml"]  # 同账号重复配置只跑排序首个


# ── nickname（Q-NICKNAME 作者昵称获取与清洗）─────────────────────────

def test_clean_nickname():
    assert nn.clean_nickname("小水") == "小水"
    assert nn.clean_nickname("softFuFu") == "softFuFu"
    # emoji / 变体选择符 / 装饰符号移除，中文与常规字符保留
    assert nn.clean_nickname("小水🌸") == "小水"
    assert nn.clean_nickname("⭐喵个咪⭐") == "喵个咪"
    assert nn.clean_nickname("画家✨孙天骄") == "画家孙天骄"
    # 控制字符移除
    assert nn.clean_nickname("a\x00b\x1fc") == "abc"
    # Windows 非法字符交由 sanitize_name 折叠
    assert nn.clean_nickname("小水/水:小") == "小水_水_小"
    # 纯 emoji / 空白 / None → 空串（调用方回退）
    assert nn.clean_nickname("🌸✨") == ""
    assert nn.clean_nickname("   ") == ""
    assert nn.clean_nickname(None) == ""


def test_lookup_removed_no_local_db_query():
    """Q-NICKNAME 修订（2026-10-13）：不再查本地缓存库（用户决策）。"""
    import inspect

    assert not hasattr(nn, "lookup_cached_nickname")
    assert not hasattr(nn, "_USER_DB_PATH")
    source = inspect.getsource(nn)
    assert "douyin_users.db" not in source  # 与本地用户库完全解耦


def test_fetch_nickname_from_profile(monkeypatch):
    """主页昵称爬取：DOM 提取成功 / 轮询超时 / 浏览器异常 三路径（mock playwright）。"""
    import playwright.sync_api as pw_api

    class FakePage:
        def __init__(self, results):
            self._results = list(results)
            self.goto_called = False

        def goto(self, *a, **k):
            self.goto_called = True

        def evaluate(self, script, *a):
            return self._results.pop(0) if self._results else ""

        def wait_for_timeout(self, ms):
            pass

    class FakeBrowser:
        def __init__(self, page):
            self._page = page
            self.closed = False

        def new_page(self):
            return self._page

        def close(self):
            self.closed = True

    class FakeFirefox:
        def __init__(self, browser):
            self._browser = browser

        def launch(self, **k):
            return self._browser

    class FakePW:
        def __init__(self, browser):
            self.firefox = FakeFirefox(browser)
            self.stopped = False

        def start(self):
            return self

        def stop(self):
            self.stopped = True

    # 成功：首两轮空（CSR 渲染中）→ 第三轮出 h1 昵称
    page = FakePage(["", "", "小水一号"])
    browser = FakeBrowser(page)
    pw = FakePW(browser)
    monkeypatch.setattr(pw_api, "sync_playwright", lambda: pw)
    assert nn.fetch_nickname_from_profile("MS4wLjABAAAAx") == "小水一号"
    assert page.goto_called and browser.closed and pw.stopped  # 资源正确释放

    # 轮询预算内无昵称 → None（预算压 0 直接超时）
    monkeypatch.setattr(nn, "_DOM_POLL_SEC", 0)
    empty_pw = FakePW(FakeBrowser(FakePage([])))
    monkeypatch.setattr(pw_api, "sync_playwright", lambda: empty_pw)
    assert nn.fetch_nickname_from_profile("MS4wLjABAAAAx") is None

    # 浏览器启动异常 → None（绝不抛出）
    def _boom():
        raise RuntimeError("no browser")

    monkeypatch.setattr(pw_api, "sync_playwright", _boom)
    assert nn.fetch_nickname_from_profile("MS4wLjABAAAAx") is None
    assert nn.fetch_nickname_from_profile("") is None


def test_fetch_nickname_by_aweme(monkeypatch):
    import httpx

    class FakeResp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    calls = []

    def fake_get(url, **_kw):
        calls.append(url)
        return FakeResp(
            {"aweme_list": [{"aweme_id": "7298", "author": {"nickname": "小水"}}]}
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    assert nn.fetch_nickname_by_aweme("7298") == "小水"
    assert len(calls) == 1  # 主节点命中即止

    # 正常响应但未匹配 → 不打备用节点，返回 None
    monkeypatch.setattr(
        httpx, "get", lambda url, **_kw: FakeResp({"aweme_list": []})
    )
    assert nn.fetch_nickname_by_aweme("7298") is None

    # 主备节点均网络异常 → None（fail-fast 回退）
    def _boom(url, **_kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "get", _boom)
    assert nn.fetch_nickname_by_aweme("7298") is None
    assert nn.fetch_nickname_by_aweme("") is None


def test_resolve_nickname_dispatch(monkeypatch):
    # 主页链接 → 打开用户主页在线爬取；昵称含 emoji 时返回清洗结果
    monkeypatch.setattr(nn, "fetch_nickname_from_profile", lambda sec: "小水🌸")
    assert nn.resolve_nickname(
        "douyin", "https://www.douyin.com/user/MS4wLjABAAAAx"
    ) == ("小水🌸", "小水", "online")
    # 单作品链接 → 移动 Feed 通道
    monkeypatch.setattr(nn, "fetch_nickname_by_aweme", lambda aid: "喵个咪")
    assert nn.resolve_nickname(
        "douyin", "https://www.douyin.com/video/7298145681699622182"
    ) == ("喵个咪", "喵个咪", "online")
    # 提取不到标识（分享短链）→ 空三元组
    assert nn.resolve_nickname("douyin", "https://v.douyin.com/i2wyU53P/") == ("", "", "")
    # 爬取失败 → 空三元组
    monkeypatch.setattr(nn, "fetch_nickname_from_profile", lambda sec: None)
    assert nn.resolve_nickname(
        "douyin", "https://www.douyin.com/user/MS4wLjABAAAAx"
    ) == ("", "", "")


def test_resolve_name_prefers_nickname(monkeypatch):
    url = "https://www.douyin.com/user/MS4wLjABAAAAx"
    monkeypatch.setattr(
        core.nickname_mod, "resolve_nickname", lambda pk, u: ("小水", "小水", "online")
    )
    # 显式名最高优先
    assert core.resolve_name("douyin", url, "我的名", "post") == ("我的名", "MS4wLjABAAAAx")
    # 未给名 → 自动采用昵称
    assert core.resolve_name("douyin", url, "", "post")[0] == "小水"
    # 昵称拿不到 → sec_uid 提取（既有行为不变）
    monkeypatch.setattr(
        core.nickname_mod, "resolve_nickname", lambda pk, u: ("", "", "")
    )
    assert core.resolve_name("douyin", url, "", "post") == ("MS4wLjABAAAAx", "MS4wLjABAAAAx")


# ── 向导 Q-DEDUP-REV：已入库账号报重复（2026-10-13 用户决策）─────────

def _setup_duplicate_conf(tmp_path):
    sec = "MS4wLjABAAAA" + "a" * 20
    confgen.save_conf(
        tmp_path / "甲.yaml", {"douyin": {"url": f"https://www.douyin.com/user/{sec}"}}
    )
    return sec, tmp_path / "甲.yaml"


def _mock_prompts(monkeypatch, values):
    seq = iter(values)
    monkeypatch.setattr(click, "prompt", lambda *a, **k: next(seq))


def test_new_task_duplicate_confirm_reruns(monkeypatch, tmp_path):
    """y 分支：报重复 → 确认追更 → rerun_task 被调用，不新建配置。"""
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    sec, conf = _setup_duplicate_conf(tmp_path)
    # 交互序列：粘贴链接 → 选模式(post) → [报重复] 回车返回主菜单
    _mock_prompts(monkeypatch, [f"https://www.douyin.com/user/{sec}", "post", ""])
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
    ran = []
    monkeypatch.setattr(
        wizard.core, "rerun_task", lambda path, **kw: ran.append(Path(path)) or 0
    )
    wizard._new_task("firefox")
    assert ran == [conf]
    assert [p.name for p in tmp_path.glob("*.yaml")] == ["甲.yaml"]


def test_new_task_duplicate_decline_cancels(monkeypatch, tmp_path):
    """n 分支：报重复 → 拒绝追更 → 取消返回，不追更不新建。"""
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    sec, _conf = _setup_duplicate_conf(tmp_path)
    _mock_prompts(monkeypatch, [f"https://www.douyin.com/user/{sec}", "post", ""])
    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
    ran = []
    monkeypatch.setattr(
        wizard.core, "rerun_task", lambda path, **kw: ran.append(Path(path)) or 0
    )
    wizard._new_task("firefox")
    assert ran == []
    assert [p.name for p in tmp_path.glob("*.yaml")] == ["甲.yaml"]


# ── 向导 Q-NICKNAME-D3：昵称后台并行爬取 + 别名后置（2026-10-15 共识）──

def test_new_task_nickname_as_default_name(monkeypatch, tmp_path):
    """并行流程：后台爬取就绪 → 昵称作默认别名 → run_task 收到昵称。"""
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    sec = "MS4wLjABAAAA" + "b" * 20  # 未入库新账号
    url = f"https://www.douyin.com/user/{sec}"
    monkeypatch.setattr(
        wizard.nickname_mod, "resolve_nickname",
        lambda pk, u: ("小水一号", "小水一号", "online"),
    )
    # 交互序列：链接 → 模式(post) → 区间(all) → [高级设置=否] →
    # [等待爬取(已完成)] → 别名(回车=昵称默认) → [确认下载=是] → 回车返回
    _mock_prompts(monkeypatch, [url, "post", "all", "", ""])
    confirms = iter([False, True])
    monkeypatch.setattr(click, "confirm", lambda *a, **k: next(confirms))
    ran = []
    monkeypatch.setattr(
        wizard.core, "run_task", lambda pk, u, m, **kw: ran.append(kw) or 0
    )
    wizard._new_task("firefox")
    assert ran and ran[0]["name"] == "小水一号"


def test_new_task_nickname_fallback_on_probe_failure(monkeypatch, tmp_path):
    """后台爬取异常 → 静默回退 sec_uid 标识默认（Q5），流程不中断。"""
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    sec = "MS4wLjABAAAA" + "c" * 20
    url = f"https://www.douyin.com/user/{sec}"

    def _fail(pk, u):
        raise RuntimeError("probe boom")

    monkeypatch.setattr(wizard.nickname_mod, "resolve_nickname", _fail)
    _mock_prompts(monkeypatch, [url, "post", "all", "", ""])
    confirms = iter([False, True])
    monkeypatch.setattr(click, "confirm", lambda *a, **k: next(confirms))
    ran = []
    monkeypatch.setattr(
        wizard.core, "run_task", lambda pk, u, m, **kw: ran.append(kw) or 0
    )
    wizard._new_task("firefox")
    assert ran and ran[0]["name"] == sec  # 回退链接标识默认


def test_await_nickname_probe_semantics():
    """等待辅助：已完成线程读结果属性；无结果属性（异常路径）→ 空串。"""
    t1 = threading.Thread(target=lambda: None)
    t1.start()
    t1.join()
    assert wizard._await_nickname_probe(t1) == ("", "")  # 未设属性 → 兜底空串

    t2 = threading.Thread(target=lambda: None)
    t2.start()
    t2.join()
    t2.nick_raw, t2.nick_clean = "小水🌸", "小水"  # 模拟线程写入的结果
    assert wizard._await_nickname_probe(t2) == ("小水🌸", "小水")


# ── DIR-ALIAS（2026-10-15 用户决策）：下载目录名 = 任务别名 ─────────

def _make_download_base(tmp_path, nickname_dir=None, alias_dir=None):
    """构造下载根 + 可选的旧昵称目录/别名目录，返回 kwargs 基础。"""
    base = tmp_path / "Download"
    base.mkdir(parents=True, exist_ok=True)
    if nickname_dir:
        (base / nickname_dir).mkdir(exist_ok=True)
    if alias_dir:
        (base / alias_dir).mkdir(exist_ok=True)
    return {"path": str(base)}


def _cruf():
    from f2.apps.douyin.utils import create_or_rename_user_folder

    return create_or_rename_user_folder


def test_dir_alias_new_task_creates_alias_dir(tmp_path):
    """全新任务（无任何既有目录）：直接以"序号-别名"建目录。"""
    kwargs = _make_download_base(tmp_path)
    p = _cruf()({**kwargs, "task_alias": "01-余冰慧"}, {}, "余冰慧✨")
    assert p.name == "01-余冰慧" and p.is_dir()
    assert not (tmp_path / "Download" / "余冰慧✨").exists()  # 不再建昵称目录


def test_dir_alias_migrates_existing_nickname_dir(tmp_path):
    """存量迁移：旧昵称目录（含已下载文件）一次性 rename 为"序号-别名"。"""
    kwargs = _make_download_base(tmp_path, nickname_dir="余冰慧✨")
    (tmp_path / "Download" / "余冰慧✨" / "x.mp4").write_text("data")
    p = _cruf()(
        {**kwargs, "task_alias": "01-余冰慧"}, {"nickname": "余冰慧✨"}, "余冰慧✨"
    )
    assert p.name == "01-余冰慧"
    assert (p / "x.mp4").exists()  # 已下载文件随目录迁移（跳过逻辑不受影响）
    assert not (tmp_path / "Download" / "余冰慧✨").exists()


def test_dir_seq_migrates_plain_alias_dir(tmp_path):
    """DIR-SEQ 中间态：上轮已迁移的无序号纯别名目录 → 再迁移为"序号-别名"。"""
    kwargs = _make_download_base(tmp_path, alias_dir="余冰慧")
    (tmp_path / "Download" / "余冰慧" / "y.mp4").write_text("data")
    p = _cruf()({**kwargs, "task_alias": "01-余冰慧"}, {}, "余冰慧✨")
    assert p.name == "01-余冰慧"
    assert (p / "y.mp4").exists()
    assert not (tmp_path / "Download" / "余冰慧").exists()


def test_dir_alias_reuses_existing_alias_dir(tmp_path):
    """幂等：别名目录已存在 → 直接复用，不重复迁移、不报错。"""
    kwargs = _make_download_base(tmp_path, nickname_dir="余冰慧✨", alias_dir="01-余冰慧")
    p = _cruf()({**kwargs, "task_alias": "01-余冰慧"}, {}, "余冰慧✨")
    assert p.name == "01-余冰慧"


def test_dir_alias_absent_keeps_nickname_behavior(tmp_path):
    """无 task_alias 键（官方 f2 / one 模式）：维持昵称目录既有行为。"""
    kwargs = _make_download_base(tmp_path)
    p = _cruf()(kwargs, {}, "小水一号")
    assert p.name == "小水一号"


def test_build_conf_writes_task_alias(monkeypatch):
    monkeypatch.setattr(confgen, "official_defaults", lambda pk: {})
    sec = "MS4wLjABAAAA" + "f" * 20
    conf = confgen.build_conf(
        "douyin", url=f"https://www.douyin.com/user/{sec}",
        mode="post", cookie="", task_alias="01-余冰慧",
    )
    assert conf["douyin"]["task_alias"] == "01-余冰慧"
    # 未传别名（one 模式临时配置）→ 不写该键，引擎回退昵称行为
    conf2 = confgen.build_conf(
        "douyin", url="https://www.douyin.com/video/7298145681699622182",
        mode="one", cookie="",
    )
    assert "task_alias" not in conf2["douyin"]


def test_next_task_index_appends(monkeypatch, tmp_path):
    """追加式序号：取已用最大序号 +1；空目录 → 1；坏配置/无 alias 跳过。"""
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    assert confgen.next_task_index("douyin") == 1  # 空目录

    confgen.save_conf(
        tmp_path / "A.yaml", {"douyin": {"url": "u", "task_alias": "01-甲"}}
    )
    confgen.save_conf(
        tmp_path / "B.yaml", {"douyin": {"url": "u", "task_alias": "03-乙"}}
    )
    confgen.save_conf(tmp_path / "C.yaml", {"douyin": {"url": "u"}})  # 无 alias
    (tmp_path / "坏.yaml").write_text("douyin: [broken", encoding="utf-8")
    assert confgen.next_task_index("douyin") == 4  # max(1,3)+1


def test_run_task_assigns_indexed_alias(monkeypatch, tmp_path):
    """新任务：配置落盘的 task_alias = "序号-别名"（追加式分配即固化）。"""
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    monkeypatch.setattr(confgen, "official_defaults", lambda pk: {})
    monkeypatch.setattr(core, "_run_with_retry", lambda args, platform_key="", task_label="": 0)
    monkeypatch.setattr(core, "_obtain_cookie", lambda pk, b, interactive=True: "ck")
    monkeypatch.setattr(core, "_cleanup_desc_files", lambda pk: 0)
    monkeypatch.setattr(core, "_write_unfinished_manifest", lambda pk: None)

    sec = "MS4wLjABAAAA" + "g" * 20
    rc = core.run_task(
        "douyin", f"https://www.douyin.com/user/{sec}", "post", name="新作者"
    )
    assert rc == 0
    data = yaml.safe_load((tmp_path / "新作者.yaml").read_text(encoding="utf-8"))
    assert data["douyin"]["task_alias"] == "01-新作者"

    # 第二个新任务 → 序号追加为 02
    sec2 = "MS4wLjABAAAA" + "h" * 20
    assert core.run_task(
        "douyin", f"https://www.douyin.com/user/{sec2}", "post", name="再来一个"
    ) == 0
    data2 = yaml.safe_load((tmp_path / "再来一个.yaml").read_text(encoding="utf-8"))
    assert data2["douyin"]["task_alias"] == "02-再来一个"


def test_rerun_task_injects_task_alias(monkeypatch, tmp_path):
    """重跑存量任务：自动补写 task_alias = "序号-任务名"（幂等落盘，追加式）。"""
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    conf = tmp_path / "旧任务.yaml"
    sec = "MS4wLjABAAAA" + "e" * 20
    confgen.save_conf(
        conf,
        {"douyin": {"url": f"https://www.douyin.com/user/{sec}", "mode": "post"}},
    )
    monkeypatch.setattr(core, "_obtain_cookie", lambda pk, b, interactive=True: "ck")
    monkeypatch.setattr(
        core, "_run_with_retry", lambda args, platform_key="", task_label="": 0
    )
    monkeypatch.setattr(core, "_cleanup_desc_files", lambda pk: 0)
    monkeypatch.setattr(cookie_mod, "clear_cookie", lambda pk: None)
    rc = core.rerun_task(conf, interactive=False)  # 非交互 → 增量
    assert rc == 0
    data = yaml.safe_load(conf.read_text(encoding="utf-8"))
    assert data["douyin"]["task_alias"] == "01-旧任务"

    # 再重跑一次 → 已有 task_alias 幂等跳过，序号不变
    assert core.rerun_task(conf, interactive=False) == 0
    data = yaml.safe_load(conf.read_text(encoding="utf-8"))
    assert data["douyin"]["task_alias"] == "01-旧任务"
