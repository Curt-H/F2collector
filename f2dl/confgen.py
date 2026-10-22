"""配置生成：官方默认（f2 --init-config）底稿 + 关键字段改写 → 任务配置（Q3/Q6）。"""

import copy
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

import yaml

from . import runner
from .apps import INTERVAL_MODES, TOOL_ROOT, canonical_user_url, get_platform, sanitize_name

_CACHE_DIR = TOOL_ROOT / "f2dl" / "_cache"


def official_defaults(platform_key: str) -> dict:
    """获取 f2 官方默认配置段（本地缓存，避免重复起子进程）。"""
    plat = get_platform(platform_key)
    cache = _CACHE_DIR / f"official_{plat['app']}.yaml"
    if not cache.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            rc = runner.run_f2(
                [plat["alias"], "--init-config", str(cache)],
                timeout=runner.SHORT_TIMEOUT,
            )
        except subprocess.SubprocessError as exc:
            # 本地补丁(D1, 2026-09-17)：TimeoutExpired 等子进程异常就地转为
            # RuntimeError——下游（cli/core/wizard）统一按可读错误处理，
            # 不再裸栈崩溃。
            raise RuntimeError(f"f2 --init-config 超时或失败: {exc}") from exc
        if rc != 0 or not cache.exists():
            raise RuntimeError("f2 --init-config 生成官方默认配置失败")
    data = yaml.safe_load(cache.read_text(encoding="utf-8")) or {}
    return data.get(plat["app"]) or {}


def build_conf(
    platform_key: str,
    *,
    url: str,
    mode: str,
    cookie: str,
    interval: str = "all",
    overrides: Optional[dict] = None,
    task_alias: Optional[str] = None,
) -> dict:
    """构造完整任务配置：官方默认为底稿，显式写入任务字段与高级设置值。

    task_alias（DIR-ALIAS，2026-10-15 用户决策）：任务别名写入配置——
    引擎（f2/apps/douyin/utils.create_or_rename_user_folder 本地补丁）
    优先用它作下载目录名，与作者昵称变化解耦；one 模式（临时配置）/
    无别名不写该键 → 引擎回退昵称目录行为。
    """
    plat = get_platform(platform_key)
    section = copy.deepcopy(official_defaults(platform_key))
    section.update(
        {
            "url": url,
            "mode": mode,
            "cookie": cookie,
            # Q18：分平台下载目录，绝对路径，不受运行时 CWD 影响
            "path": str(plat["download_dir"]),
            # one/live 不依赖日期区间，固定 all（f2 语义）
            "interval": interval if mode in INTERVAL_MODES else "all",
        }
    )
    if task_alias:
        section["task_alias"] = task_alias
    # 官方默认里布尔/数值键为空值；填入约定默认值，让配置文件所见即所得。
    # （f2 的 merge_config 会跳过空值，空键不会遮蔽包内低频默认——源码已核实。）
    for key, _kind, _label, default in plat["advanced"]:
        if section.get(key) in (None, ""):
            section[key] = default
    for key, value in (overrides or {}).items():
        if value not in (None, ""):
            section[key] = value
    return {plat["app"]: section}


def conf_path(platform_key: str, name: str) -> Path:
    plat = get_platform(platform_key)
    return plat["conf_dir"] / f"{sanitize_name(name)}.yaml"


def save_conf(path: Path, conf: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 本地补丁(C5, 2026-09-17)：原子写——先写同目录临时文件再 os.replace，
    # 防止任务被中断（Ctrl+C/断电）时截断任务配置，导致下次重跑
    # yaml 解析失败报"无法识别任务配置的平台"。
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.dump(conf, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def load_conf(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def last_values(platform_key: str, name: str) -> dict:
    """向导默认值来源：该作者已有任务配置的值（Q16：向导逐项问但带上次值）。"""
    path = conf_path(platform_key, name)
    if not path.exists():
        return {}
    return load_conf(path).get(get_platform(platform_key)["app"]) or {}


def write_temp_conf(platform_key: str, conf: dict) -> Path:
    """one 模式临时配置：系统临时目录，下载后即删（Q17）。"""
    plat = get_platform(platform_key)
    directory = Path(tempfile.gettempdir()) / "f2dl"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"tmp_{plat['app']}_{int(time.time() * 1000)}.yaml"
    save_conf(path, conf)
    return path


# ── 以账号主页为标准的防重复（Q-DEDUP，2026-09-22 共识）────────────────


def account_key_of(platform_key: str, path: Path) -> Optional[str]:
    """任务配置 → 账号标识（从其 url 字段提取 sec_user_id）。

    配置缺失/损坏/无主页标识时返回 None——判重视为"无法识别"，不让
    单个坏配置炸掉调用方（rerun-all 的批量防线同样依赖此函数）。
    """
    plat = get_platform(platform_key)
    try:
        section = (load_conf(path) or {}).get(plat["app"]) or {}
    except (OSError, yaml.YAMLError):
        return None
    norm = canonical_user_url(platform_key, str(section.get("url") or ""))
    return norm[0] if norm else None


def conf_index(platform_key: str) -> Dict[str, Path]:
    """扫描任务配置目录：账号标识 → 配置路径（同账号多份时排序首个胜出）。"""
    plat = get_platform(platform_key)
    index: Dict[str, Path] = {}
    if not plat["conf_dir"].exists():
        return index
    for path in sorted(plat["conf_dir"].glob("*.yaml")):
        key = account_key_of(platform_key, path)
        if key and key not in index:
            index[key] = path
    return index


def find_conf_by_key(platform_key: str, account_key: str) -> Optional[Path]:
    """按账号标识找已有任务配置（无 → None）。判重的唯一查询入口。"""
    return conf_index(platform_key).get(account_key)


def next_free_name(platform_key: str, base: str) -> str:
    """别名撞车（不同账号同昵称）时取第一个不存在的 -N 后缀名（防静默覆盖）。"""
    n = 1
    while conf_path(platform_key, f"{base}-{n}").exists():
        n += 1
    return f"{base}-{n}"


# ── DIR-SEQ（2026-10-15 用户决策）：下载目录名 = 序号-别名 ────────────

#: task_alias 的序号前缀（如 "01-小水一号" → 1）
_TASK_INDEX_RE = re.compile(r"^(\d+)-")


def next_task_index(platform_key: str) -> int:
    """扫描任务配置，取 task_alias 序号前缀的最大值 + 1（无任何序号 → 1）。

    追加式分配（用户决策）：新任务/存量任务首次落 alias 时取当前最大 +1，
    分配即固化写入配置，永不复用已删任务的空缺序号（简单且零连锁迁移）；
    rerun-all 批量时任务按文件名顺序依次获得 01..NN。坏配置静默跳过。
    """
    plat = get_platform(platform_key)
    best = 0
    if plat["conf_dir"].exists():
        for path in sorted(plat["conf_dir"].glob("*.yaml")):
            try:
                section = (load_conf(path) or {}).get(plat["app"]) or {}
            except Exception:  # noqa: BLE001 - 坏配置不阻塞序号扫描
                continue
            m = _TASK_INDEX_RE.match(str(section.get("task_alias") or ""))
            if m:
                best = max(best, int(m.group(1)))
    return best + 1
