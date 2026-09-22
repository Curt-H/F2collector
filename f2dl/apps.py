"""平台注册表：模式表、路径约定、链接标识提取、高级设置字段。"""

import re
from pathlib import Path
from typing import Optional, Tuple

#: 工具根目录（F2test）
TOOL_ROOT = Path(__file__).resolve().parent.parent

DOUYIN_MODES = [
    "one", "post", "like", "collection", "collects",
    "music", "mix", "live", "related", "friend",
]

#: 需要询问/生效 interval 的模式（one/live 不需要）
INTERVAL_MODES = {"post", "like", "collection", "collects", "music", "mix", "related", "friend"}

#: f2 --auto-cookie 支持的浏览器（与 f2 0.0.1.7 的 BROWSER_LIST 一致）
BROWSERS = [
    "chrome", "firefox", "edge", "opera", "opera_gx",
    "safari", "chromium", "brave", "vivaldi", "librewolf",
]

# 高级设置字段: (键, 类型bool/int/str, 中文标签, 默认值)
# 默认值取自 f2 0.0.1.7 包内 conf/app.yaml（低频默认）；
# 布尔项按"只保留媒体文件（图片/视频）"的需求显式置假：folderize 平铺、
# music/cover/desc/lyric 全关。
# naming 默认不含 {desc}：f2 仅将 desc 截断到 200 字符（win32），而完整路径
# （Download\...\作者\{naming}\{naming}_desc.txt）叠加后极易超过 Windows
# MAX_PATH=260 限制，导致 FileNotFoundError；文案内容本就会存入 _desc.txt，
# 用定长的 {aweme_id} 命名既唯一又安全。
DOUYIN_ADVANCED = [
    ("folderize", "bool", "按作品分文件夹保存", False),
    ("music", "bool", "保存视频原声", False),
    ("cover", "bool", "保存视频封面", False),
    ("desc", "bool", "保存视频文案", False),
    ("lyric", "bool", "保存原声歌词(.lrc)", False),
    ("naming", "str", "文件命名规则", "{create}_{aweme_id}"),
    ("path", "str", "下载根目录", "Download"),  # PATH-1：相对路径（相对工具根）
    ("max_counts", "int", "最大下载数(0不限)", 0),
    ("max_tasks", "int", "异步任务数", 10),
    ("max_connections", "int", "并发连接数", 5),
    ("page_counts", "int", "每页获取作品数", 20),
    ("page_interval", "int", "翻页间隔(秒,防风控)", 45),
    ("timeout", "int", "请求超时(秒)", 20),
    ("max_retries", "int", "超时重试次数", 5),
]
PLATFORMS = {
    "douyin": {
        "key": "douyin", "app": "douyin", "alias": "dy", "label": "抖音",
        "modes": DOUYIN_MODES,
        "conf_dir": TOOL_ROOT / "dy" / "conf",
        "cookie_file": TOOL_ROOT / "dy" / "cookie.yaml",
        # PATH-1(2026-09-18)：相对路径（相对工具根；runner 已把 f2 子进程
        # CWD 固定为工具根），项目搬迁/换机不再产生指向旧根的绝对路径。
        "download_dir": Path("Download"),
        "advanced": DOUYIN_ADVANCED,
    },
}

# 从链接提取作者/作品标识的正则（Q13：别名优先，缺省自动提取）
_ID_PATTERNS = {
    "douyin": [
        re.compile(r"(MS4wLjABAAAA[\w-]+)"),
        re.compile(r"/(?:video|note)/(\d+)"),
    ],
}

_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"COM{i}" for i in range(1, 10)
} | {f"LPT{i}" for i in range(1, 10)}


def get_platform(key: str) -> dict:
    try:
        return PLATFORMS[key]
    except KeyError:
        raise ValueError(f"未知平台: {key}（支持: {', '.join(PLATFORMS)}）")


def extract_identifier(platform_key: str, url: str) -> Optional[str]:
    """从链接提取作者标识（sec_user_id / 微博 uid）或单作品 id，失败返回 None。"""
    for pattern in _ID_PATTERNS.get(platform_key, []):
        match = pattern.search(url or "")
        if match:
            return match.group(1)
    return None


def sanitize_name(name: str) -> str:
    """清理为安全的配置文件名（保留中文）。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t ]+', "_", (name or "").strip()).strip("._")
    if not cleaned:
        raise ValueError("任务别名为空")
    if cleaned.upper() in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    return cleaned


#: 抖音用户主页规范 URL 模板（判重与落盘统一用它，剥掉复制来源的追踪参数）
_DOUYIN_USER_URL = "https://www.douyin.com/user/{sec_uid}"


def canonical_user_url(platform_key: str, url: str) -> Optional[Tuple[str, str]]:
    """主页链接 → (账号标识, 规范主页 URL)；非主页链接返回 None。

    以账号主页为标准的防重复（Q-DEDUP，2026-09-22 共识）共用入口：
    账号唯一键取首个标识模式的捕获组（抖音为 sec_user_id，URL 带追踪
    参数也能稳定提取）；返回的规范 URL 剥掉 ?from_tab_name=... 等查询
    参数——判重比较与配置落盘一律用它，消除"同一账号 URL 字面不同"的
    隐患。作品链接/短链/无法识别 → None，调用方跳过判重。
    """
    patterns = _ID_PATTERNS.get(platform_key) or []
    if not patterns:
        return None
    match = patterns[0].search(url or "")
    if not match:
        return None
    key = match.group(1)
    if platform_key == "douyin":
        return key, _DOUYIN_USER_URL.format(sec_uid=key)
    return None
