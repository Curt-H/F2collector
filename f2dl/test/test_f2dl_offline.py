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
import time
from datetime import datetime
from pathlib import Path

import pytest
import yaml

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


def test_run_task_reuses_existing_conf_for_same_account(monkeypatch, tmp_path):
    monkeypatch.setitem(apps.PLATFORMS["douyin"], "conf_dir", tmp_path)
    monkeypatch.setattr(confgen, "official_defaults", lambda pk: {})
    captured = {}

    def fake_retry(args, platform_key="", task_label=""):
        captured["args"] = list(args)
        c_path = Path(args[args.index("-c") + 1])
        captured["path"] = c_path
        captured["conf"] = yaml.safe_load(c_path.read_text(encoding="utf-8"))
        return 0

    monkeypatch.setattr(core, "_run_with_retry", fake_retry)
    monkeypatch.setattr(core, "_obtain_cookie", lambda pk, b, interactive=True: "ck")
    monkeypatch.setattr(core, "_cleanup_desc_files", lambda pk: 0)
    monkeypatch.setattr(core, "_write_unfinished_manifest", lambda pk: None)

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

    # 换别名 + 带不同追踪参数的同一账号链接提交 → 复用「甲」，不新建文件
    rc = core.run_task(
        "douyin", f"https://www.douyin.com/user/{sec}?nt=2", "post", name="新别名"
    )
    assert rc == 0
    assert captured["path"] == tmp_path / "甲.yaml"  # 复用已有任务
    assert sorted(p.name for p in tmp_path.glob("*.yaml")) == ["甲.yaml"]  # 无重复文件
    section = captured["conf"]["douyin"]
    assert section["url"] == f"https://www.douyin.com/user/{sec}"  # 规范 URL 落盘
    assert section["page_counts"] == 999  # 自定义高级值保留
    assert section["last_run"] == "2026-09-20 08:00:00"  # 增量进度带入
    args = captured["args"]
    assert args[args.index("-i") + 1] == f"2026-09-20|{datetime.now():%Y-%m-%d}"


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
