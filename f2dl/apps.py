"""平台注册表：模式表、路径约定、链接标识提取、高级设置字段。"""

import re
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

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


# ── 链接合规检查（Q-URLCHECK，2026-10-13）─────────────────────────────
# 用户粘贴的链接先验后用：非抖音链接（B站/微博/TikTok 等、仿冒域名、
# 非 URL 纯文本）在输入处直接拒绝，避免白白等待 cookie 抓取（5~12s）
# 与浏览器启动后才在 f2 阶段报错。

#: 各平台允许的根域名（含其全部子域）。抖音域名家族与 vendored f2
#: （f2/apps/douyin/utils.py）实际消费的入口一致：
#: www.douyin.com 主站 / v.douyin.com 分享短链 / live.douyin.com 直播 /
#: www.iesdouyin.com 移动分享域。
_ALLOWED_ROOT_DOMAINS = {
    "douyin": ("douyin.com", "iesdouyin.com"),
}

#: 允许的协议（拦截 javascript:/file:/data: 等被当"链接"注入的输入）
_ALLOWED_SCHEMES = ("http", "https")

#: 带 scheme 的输入形态（直接粘贴常丢协议，需容错补 https://）
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _host_matches(host: str, root_domains: tuple) -> bool:
    """域名精确匹配：host 等于根域名本身，或是其子域（大小写不敏感）。

    禁用子串 `in` 式匹配——`douyin.com.evil.com`、
    `evil.com/?u=www.douyin.com` 等仿冒形态必须拦下。
    """
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in root_domains)


def allowed_hosts_label(platform_key: str) -> str:
    """面向用户的域名白名单描述（错误提示用）。"""
    return " / ".join(_ALLOWED_ROOT_DOMAINS.get(platform_key, ()))


def validate_url(platform_key: str, url: Optional[str]) -> str:
    """链接合规检查：仅平台合法链接可过（返回规范化后的 URL）。

    校验分层：
    - 空 / 非 URL 纯文本 → ValueError；
    - 缺协议（如 www.douyin.com/user/xxx）→ 容错补 https:// 后放行；
    - 协议非 http/https → ValueError；
    - 域名不在平台域名家族（他站/仿冒域名）→ ValueError。

    只看域名与 URL 形态，不要求能提取账号标识——v.douyin.com 短链
    不含 sec_uid 但合法；标识提取 / 短链跳转解析仍由 f2 负责。
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError(
            "链接为空，请粘贴抖音链接（主页 / 单作品 / 分享短链 / 直播等）"
        )
    candidate = raw if _SCHEME_RE.match(raw) else f"https://{raw}"
    parsed = urlparse(candidate)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"链接协议仅支持 http/https，检测到: {scheme or '无'}")
    if not host:
        raise ValueError(
            "这看起来不是链接，请粘贴以 http(s):// 开头的完整链接"
        )
    root_domains = _ALLOWED_ROOT_DOMAINS.get(platform_key)
    if root_domains is None:
        # 平台未配置白名单（预留）：不做域名限制
        return candidate
    if not _host_matches(host, root_domains):
        raise ValueError(
            f"不是抖音链接：检测到域名 {host}，"
            f"仅支持 {allowed_hosts_label(platform_key)}（及其子域）的链接"
        )
    return candidate
