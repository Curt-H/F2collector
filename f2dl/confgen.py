"""配置生成：官方默认（f2 --init-config）底稿 + 关键字段改写 → 任务配置（Q3/Q6）。"""

import copy
import tempfile
import time
from pathlib import Path
from typing import Optional

import yaml

from . import runner
from .apps import INTERVAL_MODES, TOOL_ROOT, get_platform, sanitize_name

_CACHE_DIR = TOOL_ROOT / "f2dl" / "_cache"


def official_defaults(platform_key: str) -> dict:
    """获取 f2 官方默认配置段（本地缓存，避免重复起子进程）。"""
    plat = get_platform(platform_key)
    cache = _CACHE_DIR / f"official_{plat['app']}.yaml"
    if not cache.exists():
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        rc = runner.run_f2(
            [plat["alias"], "--init-config", str(cache)],
            timeout=runner.SHORT_TIMEOUT,
        )
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
) -> dict:
    """构造完整任务配置：官方默认为底稿，显式写入任务字段与高级设置值。"""
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
    path.write_text(yaml.dump(conf, allow_unicode=True, sort_keys=False), encoding="utf-8")


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
