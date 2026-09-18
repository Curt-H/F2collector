"""任务编排：每任务强制重抓 cookie → 配置生成/复用 → 调 f2 下载（--cookie 直传）→ 清理。

cookie 生命周期（Q2b/Q3b/Q4a 共识）：配置文件 cookie 字段永久为空；
新 cookie 由 f2 原生 --cookie CLI 参数直传下载进程；任务路径结束
（无论成败/中断）一律清空 cookie 存储，下个任务启动时重新抓取。
"""

import copy
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import click
import yaml
from rich.markup import escape

from . import confgen, runner
from . import console
from . import cookie as cookie_mod
from .apps import (
    INTERVAL_MODES,
    PLATFORMS,
    TOOL_ROOT,
    extract_identifier,
    get_platform,
    sanitize_name,
)

_INTERVAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\|\d{4}-\d{2}-\d{2}$")
_RUN_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _now_str() -> str:
    return datetime.now().strftime(_RUN_TIME_FMT)


def validate_interval(value: str) -> str:
    value = (value or "all").strip()
    if value == "all" or _INTERVAL_RE.match(value):
        return value
    raise ValueError("interval 格式应为 all 或 YYYY-MM-DD|YYYY-MM-DD")


def _resolve_incremental(platform_key: str, section: dict) -> Tuple[str, str]:
    """解析任务配置 section 的 last_run 与增量窗口（直跑/重跑共用，勿再各自内联）。

    返回 (last_run, window)：
    - last_run：合法的上次运行时间；缺失或格式非法时为 ""（降级为全量）；
    - window：仅 douyin 产出 "上次运行日|今天"——引擎把它转成分页游标，
      翻到窗口起点即停（handler.py 的 max_cursor < min_cursor 即 break），
      窗口外的旧作品不再发起 API 请求。
    """
    candidate = str((section or {}).get("last_run") or "")
    try:
        datetime.strptime(candidate, _RUN_TIME_FMT)
    except ValueError:
        return "", ""
    window = f"{candidate.split(' ')[0]}|{datetime.now():%Y-%m-%d}"
    return candidate, window


def refresh_cookie(platform_key: str, browser: str = "firefox") -> str:
    """强制重抓 cookie（Q4a）：清存储（自愈强杀残留）→ 浏览器预检 → f2 auto-cookie。

    成功返回新 cookie；预检不过 / 抓取失败 / 结果为空抛 CookieNotReadyError
    （BrowserRunningError 为其子类）。
    """
    plat = get_platform(platform_key)
    cookie_mod.clear_cookie(platform_key)
    cookie_mod.ensure_browser_closed(browser)
    console.print(f"[dim]从 {browser} 重抓 {plat['label']} cookie（f2 --auto-cookie）……[/dim]")
    ok, cookie = cookie_mod.grab_cookie(platform_key, browser)
    if not ok or not cookie:
        raise cookie_mod.CookieNotReadyError(
            f"从 {browser} 抓取 {plat['label']} cookie 失败"
            "（常见原因：浏览器未完全关闭、未登录目标站点、cookie 库为空）"
        )
    console.print(f"[green]cookie 抓取成功: {cookie_mod.mask(cookie)}[/green]")
    return cookie


def _obtain_cookie(platform_key: str, browser: str = "firefox", interactive: bool = True) -> str:
    """每个任务启动都重抓 cookie（Q4a）；交互场景失败可重试/改粘贴（Q5①）。

    interactive=False（批量追更）时失败直接抛 CookieNotReadyError，
    由 rerun_all 中止整批（Q5②）。
    """
    while True:
        try:
            return refresh_cookie(platform_key, browser)
        except cookie_mod.CookieNotReadyError as exc:
            if not interactive:
                raise
            console.print(f"[red]{escape(str(exc))}[/red]")
            choice = click.prompt(
                "关闭浏览器后回车重试 / 输入 p 改为手动粘贴 / 输入 q 放弃",
                default="", show_default=False,
            ).strip().lower()
            if choice == "q":
                raise cookie_mod.CookieNotReadyError("已放弃获取 cookie") from None
            if choice == "p":
                try:
                    raw = click.prompt("粘贴 cookie")
                    cookie = cookie_mod.paste_cookie(platform_key, raw)
                except ValueError as paste_exc:
                    console.print(f"[red]{escape(str(paste_exc))}[/red]")
                    continue
                console.print("[green]粘贴的 cookie 仅用于本次任务，任务结束即清空。[/green]")
                return cookie
            # 空回车 = 用户已关闭浏览器，重试抓取


#: 批次级 cookie 缓存（本地补丁 O2，2026-09-16）：{platform_key: (cookie, 抓取时刻)}
#: 批量追更同一批任务共用新鲜 cookie（≤30 分钟），省去每任务 5~12s 的重抓；
#: 批次结束由 rerun_all 统一清空（内存缓存 + 落盘存储），不改变
#: "cookie 不落盘过夜"的安全生命周期。
_BATCH_COOKIE_TTL_SEC = 30 * 60
_BATCH_COOKIE_CACHE: Dict[str, Tuple[str, float]] = {}


def _batch_get_cookie(platform_key: str, browser: str) -> str:
    """批次模式取 cookie：缓存新鲜（≤30 分钟）直接复用，超龄/缺失重抓。"""
    cached = _BATCH_COOKIE_CACHE.get(platform_key)
    if cached and time.time() - cached[1] <= _BATCH_COOKIE_TTL_SEC:
        console.print("[dim]批次内复用新鲜 cookie（免重抓）……[/dim]")
        return cached[0]
    cookie = refresh_cookie(platform_key, browser)
    _BATCH_COOKIE_CACHE[platform_key] = (cookie, time.time())
    return cookie


def resolve_name(platform_key: str, url: str, name: str, mode: str) -> Tuple[Optional[str], str]:
    """解析任务名（Q13：别名优先，缺省自动提取；one 模式允许无名 → 临时配置）。"""
    extracted = extract_identifier(platform_key, url) or ""
    if name:
        return sanitize_name(name), extracted
    if extracted:
        return extracted, extracted
    if mode == "one":
        return None, ""
    raise ValueError("无法从链接提取作者标识，且未提供 -n/--name 别名；请用 -n/--name 指定任务别名")


#: 下载失败后的自动重试次数与退避间隔(指数退避)
#: vendored f2 已打补丁：下载流中断(httpx.ReadError 等)先在 f2 进程内做
#: 单文件级重试与 .tmp 断点续传；仍失败则以非零退出码干净退出并列出失败
#: 清单。此处对整个任务重跑兜底：已下载文件按存在性跳过、中断文件从
#: .tmp 断点续传，每次重试只补漏。
_RETRY_DELAYS_SEC = (30, 60, 120)

# 本地补丁(f2dl)：抖音任务级失败冷却。
# 历史：2026-09-15 httpx 直连列表接口存在 Argus"小时级时段性收紧"窗口
# （14:03 起 post 接口连续 8/8 全拒，16:47 未恢复），当时将冷却拉长到
# 小时级(10min→30min→1h)。2026-09-16 起 post 列表已改走 Firefox 浏览器
# 通道（browser_fetch，不经 Argus），profile 也被列表数据取代（O3）——
# 任务对 Argus 的暴露面基本清零，失败多剩 CDN 断流等瞬时原因，
# 小时级冷却只剩无谓等待。按用户要求缩短为 60/90/120 秒逐级放大；
# 已下载文件按存在性跳过、中断文件断点续传，重试只补漏。
# (Douyin task-level retry cool-down shortened to 60/90/120s: the browser
#  channel removed the Argus exposure that once justified hour-level waits.)
_DOUYIN_RETRY_DELAYS_SEC = (60, 90, 120)


#: 403 风控特征（用于检测上次运行输出并触发 token 刷新）
_RISK_403_RE = re.compile(r"(403 Forbidden|Status Code: 403|HTTP\s*403)")

#: 风控统计清单（每次 f2 运行追加一行，用于观察风控趋势）
_RISK_STATS_FILE = TOOL_ROOT / "logs" / "risk_stats.csv"


def _looks_like_403(output: str) -> bool:
    return bool(_RISK_403_RE.search(output or ""))


def _append_risk_stats(platform_key: str, task_label: str, rc: int, output: str) -> None:
    """把本次运行结果追加到 logs/risk_stats.csv（时间,平台,任务,退出码,403次数）。

    统计失败绝不影响任务（静默跳过）。
    """
    try:
        is_new = not _RISK_STATS_FILE.exists()
        _RISK_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _RISK_STATS_FILE.open("a", encoding="utf-8", newline="") as f:
            if is_new:
                f.write("time,platform,task,rc,count_403\n")
            count = len(_RISK_403_RE.findall(output or ""))
            f.write(f"{_now_str()},{platform_key or ''},{task_label},{rc},{count}\n")
    except OSError:
        pass


def _run_with_retry(args: List[str], platform_key: str = "", task_label: str = "") -> int:
    """运行 f2 下载命令，非零退出按退避序列自动重跑整个任务。

    幂等：已下载文件被 f2 按存在性跳过，中断文件从 .tmp 断点续传，
    每次重试只补漏。仅当最终成功（返回 0）时调用方才记录 last_run。
    重试等待期若检测到 403 风控特征，提示用户更新 cookie（每任务启动本就会强制重抓）；每次运行结果
    追加 logs/risk_stats.csv。

    本地补丁(f2dl)：退避序列按平台选择——抖音 Web 列表接口的 403 属
    Argus"时段性收紧"(小时级)，短退避必死，改用小时级长冷却；其余平台
    保持原指数短退避。整任务重跑即新进程，签名/指纹每次天然轮换。
    (Per-platform backoff: hour-level cool-down for Douyin's Argus
    tightening windows; other platforms keep the short escalating series.)
    """
    if platform_key == "douyin":
        delays = _DOUYIN_RETRY_DELAYS_SEC
    else:
        delays = _RETRY_DELAYS_SEC
    retries = len(delays)
    rc, output = runner.run_f2_captured(args)
    attempt = 0
    while rc != 0 and attempt < retries:
        delay = delays[min(attempt, len(delays) - 1)]
        attempt += 1
        if platform_key and _looks_like_403(output):
            if platform_key == "douyin":
                console.print(
                    "[yellow]检测到抖音 403 风控特征——任务将在冷却后自动重跑"
                    f"（第 {attempt}/{retries} 次，{delay} 秒后，退避序列 60/90/120 秒）："
                    "已下载作品自动跳过、中断文件断点续传。若 3 轮重试后仍失败，"
                    "多为 Argus 时段性收紧窗口（历史实测可持续 1~3 小时），"
                    "建议稍后再跑；期间可 Ctrl+C 退出，之后重跑同一任务自动补拉。[/yellow]"
                )
            else:
                console.print(
                    "[yellow]检测到 403 风控特征——本次 cookie 为任务启动时新抓，"
                    "仍 403 多为风控冷却或浏览器未登录目标站点；"
                    "可确认 Firefox 登录态后稍候重跑。[/yellow]"
                )
        console.print(
            f"[yellow]下载失败，{delay} 秒后进行第 {attempt}/{retries} 次"
            f"重试（已下载的自动跳过，中断文件断点续传）……[/yellow]"
        )
        time.sleep(delay)
        rc, output = runner.run_f2_captured(args)
    _append_risk_stats(platform_key, task_label, rc, output)
    return rc


def _write_unfinished_manifest(platform_key: str) -> None:
    """最终仍失败时，扫描平台下载目录中的 .tmp 断点并写入未完成清单。

    清单写入 logs/unfinished_<时间戳>.txt。.tmp 是合法续传基础，不自动
    删除；下次运行对应任务时会自动跳过已完成文件并从断点续传补齐。
    """
    plat = get_platform(platform_key)
    download_dir = plat["download_dir"]
    if not download_dir.exists():
        return
    tmp_files = sorted(download_dir.rglob("*.tmp"))
    if not tmp_files:
        console.print("[green]下载目录中没有 .tmp 断点残留，所有文件均已完整。[/green]")
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest = TOOL_ROOT / "logs" / f"unfinished_{stamp}.txt"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        "\n".join(str(p) for p in tmp_files) + "\n", encoding="utf-8"
    )
    console.print(f"[red]下载目录中仍有 {len(tmp_files)} 个 .tmp 断点未完成：[/red]")
    for p in tmp_files[:10]:
        console.print(f"  [dim]· {escape(str(p))}[/dim]")
    if len(tmp_files) > 10:
        console.print(f"  [dim]…… 其余 {len(tmp_files) - 10} 个见清单文件[/dim]")
    console.print(
        f"[yellow]未完成清单已写入: {escape(str(manifest))}（重跑对应任务可自动断点续传）[/yellow]"
    )


def _cleanup_desc_files(platform_key: str) -> int:
    """下载后清理 *_desc.txt（历史微博端硬编码保存的文案清扫；douyin 端 desc=false 本就无扰动，保留兜底）。

    douyin 端因 conf desc=false 本就不生成，此清理对 douyin 是无害空扫描。
    """
    plat = get_platform(platform_key)
    download_dir = plat["download_dir"]
    if not download_dir.exists():
        return 0
    removed = 0
    for file in download_dir.rglob("*_desc.txt"):
        try:
            file.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def run_task(
    platform_key: str,
    url: str,
    mode: str,
    *,
    name: Optional[str] = None,
    interval: str = "all",
    browser: str = "firefox",
    overrides: Optional[dict] = None,
    gen_only: bool = False,
    interactive: bool = True,
) -> int:
    """完整任务流：校验 → cookie → 生成配置 → 调 f2（gen_only 时只生成配置）。"""
    plat = get_platform(platform_key)
    if not url:
        raise ValueError("缺少下载链接（-u/--url）")
    if mode not in plat["modes"]:
        raise ValueError(
            f"平台 {platform_key} 不支持模式 {mode}（可用：{'/'.join(plat['modes'])}）"
        )
    interval = validate_interval(interval)
    task_name, _extracted = resolve_name(platform_key, url, name, mode)
    # Q17：one 模式一律用临时配置（即使能提取到作品 id 也不入库）
    is_temp = mode == "one"

    # last_run 读取与保留（douyin 端 interval 真实生效——handler 转分页游标）。
    # 必须在 build_conf 覆盖配置文件之前读取，并通过 overrides 保留写入——
    # 对所有持久任务生效（含失败场景：失败不更新，但旧值不得丢失）。
    extra_args: List[str] = []
    last_run, window = "", ""
    if not is_temp and task_name is not None:
        prev_path = confgen.conf_path(platform_key, task_name)
        if prev_path.exists():
            prev_section = confgen.load_conf(prev_path).get(plat["app"]) or {}
            last_run, window = _resolve_incremental(platform_key, prev_section)
        if last_run:
            overrides = dict(overrides or {})
            overrides.setdefault("last_run", last_run)
            if window and interval == "all" and not gen_only:
                extra_args = ["-i", window]
                console.print(
                    f"[dim]增量模式：自 {last_run} 起拉取（{window}），已下载作品仍会自动跳过[/dim]"
                )

    # Q2b：cookie 永不写入任务配置——配置里恒为空占位（f2 merge 跳过空值），
    # 新 cookie 由 --cookie CLI 直传下载进程（CLI 优先级最高，源码已核实）。
    cookie = ""
    if not gen_only:
        cookie = _obtain_cookie(platform_key, browser, interactive=interactive)
        extra_args = [*extra_args, "--cookie", cookie]
    conf = confgen.build_conf(
        platform_key, url=url, mode=mode, cookie="", interval=interval,
        overrides=overrides,
    )

    if is_temp:
        # one 模式：临时配置，用完即删（Q17）
        path = confgen.write_temp_conf(platform_key, conf)
        if gen_only:
            console.print(f"[green]临时配置已生成（one 模式不入库，保留供查看）: {path}[/green]")
            return 0
        console.print(f"[dim]临时配置: {escape(str(path))}（下载后自动删除）[/dim]")
    else:
        path = confgen.conf_path(platform_key, task_name)
        existed = path.exists()
        confgen.save_conf(path, conf)
        console.print(f"任务配置已{'更新' if existed else '生成'}: {escape(str(path))}")
        if gen_only:
            console.print("[green]配置已生成（未下载）。[/green]")
            return 0
        console.print("[dim]追更提示：之后重跑同一命令（或向导菜单 2）即可增量下载，已下载作品自动跳过。[/dim]")

    console.rule(f"{plat['label']} · {mode} · 开始下载")
    try:
        rc = _run_with_retry(
            [plat["alias"], *extra_args, "-c", str(path)],
            platform_key=platform_key,
            task_label=task_name or mode,
        )
    finally:
        # Q3b：无论成败/中断，任务路径结束必清空 cookie 存储
        if not gen_only:
            cookie_mod.clear_cookie(platform_key)
            console.print("[dim]已清空 cookie 存储（下个任务启动时重新抓取）。[/dim]")
    if is_temp:
        path.unlink(missing_ok=True)
    cleaned = _cleanup_desc_files(platform_key)
    if cleaned:
        console.print(f"[dim]已清理 {cleaned} 个 _desc.txt 文案文件。[/dim]")
    if rc != 0:
        _write_unfinished_manifest(platform_key)
    if rc == 0:
        if not is_temp and task_name is not None:
            conf[plat["app"]]["last_run"] = _now_str()
            confgen.save_conf(path, conf)
            console.print(f"[dim]已记录本次运行时间: last_run={conf[plat['app']]['last_run']}[/dim]")
        console.print("[green]下载完成。[/green]")
    else:
        console.print(f"[red]f2 退出码 {rc}，详情见 logs/ 下最新日志。[/red]")
    return rc


def rerun_task(
    conf_file: Path,
    *,
    browser: str = "firefox",
    interactive: bool = True,
    batch_mode: bool = False,
    update_mode: Optional[str] = None,
) -> int:
    """重跑已有任务配置（已存在文件自动跳过，重试只补漏）。

    与 run_task 同一 cookie 生命周期：启动强制重抓 → --cookie 直传 →
    结束（无论成败/中断）清空存储。

    本地补丁(O2)：batch_mode=True（批量追更）时改用批次级 cookie 复用
    （30 分钟新鲜度，超龄自动重抓），且任务结束不清存储——由 rerun_all
    在批次结束时统一清空。单任务/向导路径行为完全不变。

    本地补丁(2026-09-16, 双模式重跑)：update_mode 决定拉取范围——
    - "incremental"（增量，默认）：任务有 last_run 且未显式指定区间时，
      注入 -i "last_run日|今天" 只拉窗口内作品；显式区间任务尊重原区间。
    - "full"（全量）：注入 -i all 临时覆盖任何区间，从最早作品全量扫描；
      已下载文件仍按存在性自动跳过（全量≠重新下载），受 30 分钟滚动
      预算保护。配置文件中的 interval 原值任何情况下都不被修改。
      缺陷修复(2026-09-17, FULL-1)：全量改跑剥离 last_run 的临时配置副本
      ——引擎双轨语义会把"interval=all + last_run"解释成增量下限，直接
      传真实配置会让全量被静默截断成增量（主页计数核对也因
      min_cursor!=0 不运行）；临时副本使引擎落入"皆无=0"全量轨，核对
      激活，真实配置的 last_run/interval 不受影响，成功后照常回写推进。
    update_mode=None 且 interactive 且非批次时，重跑前交互询问（回车默认
    增量）；任务无 last_run 记录时增量与全量等价，不询问直接全量；
    非交互场景未指定时默认增量（自动化脚本兼容）。任务成功后 last_run
    照常推进（两种方式一致）。
    """
    conf_file = Path(conf_file)
    if not conf_file.exists():
        raise FileNotFoundError(f"任务配置不存在: {conf_file}")
    data = yaml.safe_load(conf_file.read_text(encoding="utf-8")) or {}
    if "douyin" not in data:
        raise ValueError(
            f"无法识别任务配置的平台: {conf_file}"
            "（本工具自 2026-09-16 起专注抖音，历史微博任务配置不再支持）"
        )
    alias = "dy"
    platform_key = "douyin"
    section = data.get(platform_key) or {}

    # 增量窗口解析（提前到询问前——提示文案需要 last_run 信息）
    last_run, window = _resolve_incremental(platform_key, section)
    explicit_interval = str(section.get("interval") or "all") != "all"

    # 双模式询问（2026-09-16 共识）：交互终端（stdin=tty）的单任务重跑前
    # 询问，回车默认增量；批量由 rerun_all 开头问一次后整批传入
    # update_mode；非交互（脚本/计划任务，stdin 非 tty）或未指定 → 默认
    # 增量（自动化脚本兼容，Q2）。注意 interactive 参数只控制"失败时可否
    # 交互回退"，不代表 stdin 可交互，故 tty 判定单独用 isatty。
    stdin_tty = sys.stdin.isatty() if sys.stdin else False
    if update_mode is None:
        if interactive and not batch_mode and last_run and stdin_tty:
            choice = click.prompt(
                f"重跑方式：1.增量更新（自上次运行 {last_run} 起，回车默认）"
                "  2.全量更新（扫描全部作品，已下载自动跳过，可能耗时较长）",
                type=click.Choice(["1", "2"]),
                default="1",
                show_default=False,
            )
            update_mode = "incremental" if choice == "1" else "full"
        elif interactive and not batch_mode:
            # 尚无运行记录：增量与全量等价（都是全量扫描），不询问
            update_mode = "full"
            console.print(
                "[dim]该任务尚无运行记录，本次将为全量扫描"
                "（已下载作品自动跳过）。[/dim]"
            )
        else:
            update_mode = "incremental"
    if update_mode not in ("incremental", "full"):
        raise ValueError(f"未知 update_mode: {update_mode}")

    if batch_mode:
        cookie = _batch_get_cookie(platform_key, browser)
    else:
        cookie = _obtain_cookie(platform_key, browser, interactive=interactive)

    # 按重跑方式构建区间参数：
    extra_args: List[str] = []
    # 本次运行实际传给 f2 的配置文件：全量模式指向剥离 last_run 的临时
    # 副本（见下），其余模式指向真实任务配置。
    run_conf: Path = conf_file
    if update_mode == "full":
        # 全量：-i all 临时覆盖任何显式区间（CLI 参数优先级最高，
        # 配置文件中的 interval 原值不动）。
        extra_args = ["-i", "all"]
        # 缺陷修复(2026-09-17, FULL-1)：引擎双轨语义
        # (handler.handle_user_post) 把 "interval=all + conf 含 last_run"
        # 解释为增量窗口下限——直接传真实配置会让全量被 last_run 静默
        # 截断成增量，且主页计数核对 (min_cursor==0 门控) 不会运行。
        # 改为针对剥离 last_run 的临时副本运行：引擎落入"皆无=0"全量轨，
        # 核对激活；真实配置的 last_run/interval 均不受影响，成功后
        # last_run 照常回写推进。
        full_data = copy.deepcopy(data)
        full_section = next(iter(full_data.values()), None)
        if isinstance(full_section, dict):
            full_section.pop("last_run", None)
        run_conf = confgen.write_temp_conf(platform_key, full_data)
        console.print(
            "[dim]全量更新：扫描全部作品（已下载自动跳过，"
            "滚动受 30 分钟预算保护；临时配置已剥离 last_run，"
            "引擎按零窗口下限全量扫描并核对主页计数）……[/dim]"
        )
    elif explicit_interval:
        # 增量 + 显式区间任务：尊重原区间，不注入 last_run 窗口
        console.print(
            f"[dim]增量更新：维持任务原区间 {section.get('interval')}"
            "（不注入 last_run 窗口）[/dim]"
        )
    elif window:
        # 增量：last_run 窗口注入（窗口外的旧作品不再发起 API 请求）
        extra_args = ["-i", window]
        console.print(
            f"[dim]增量更新：自 {last_run} 起拉取（{window}），"
            "已下载作品仍会自动跳过[/dim]"
        )
    # Q2b：新 cookie 走 CLI 直传，配置文件里的 cookie 字段保持为空
    extra_args = [*extra_args, "--cookie", cookie]
    console.rule(f"重跑任务 · {conf_file.name}")
    try:
        rc = _run_with_retry(
            [alias, *extra_args, "-c", str(run_conf)],
            platform_key=platform_key,
            task_label=conf_file.stem,
        )
    finally:
        if run_conf != conf_file:
            run_conf.unlink(missing_ok=True)  # 全量临时副本即用即删
        # Q3b：无论成败/中断，任务路径结束必清空 cookie 存储；
        # 批次模式(O2)例外——留存复用，由 rerun_all 批次结束统一清空。
        if batch_mode:
            console.print(
                "[dim]批次模式：cookie 留存复用，批次结束统一清空。[/dim]"
            )
        else:
            cookie_mod.clear_cookie(platform_key)
            console.print("[dim]已清空 cookie 存储（下个任务启动时重新抓取）。[/dim]")
    cleaned = _cleanup_desc_files(platform_key)
    if cleaned:
        console.print(f"[dim]已清理 {cleaned} 个 _desc.txt 文案文件。[/dim]")
    if rc != 0:
        _write_unfinished_manifest(platform_key)
    if rc == 0:
        app_section = next(iter(data.values()), None)
        if isinstance(app_section, dict):
            app_section["last_run"] = _now_str()
            confgen.save_conf(conf_file, data)
            console.print(f"[dim]已记录本次运行时间: last_run={app_section['last_run']}[/dim]")
        console.print("[green]任务完成。[/green]")
    else:
        console.print(f"[red]f2 退出码 {rc}，详情见 logs/ 下最新日志。[/red]")
    return rc


def rerun_all(update_mode: Optional[str] = None) -> int:
    """批量追更全部任务（dy→wb 顺序），任务间随机冷却，单个失败不中断。

    本地补丁(2026-09-16, 双模式重跑)：update_mode 整批统一透传给每个
    rerun_task（"incremental"/"full"）；None 时由 rerun_task 内部按
    非交互默认取增量。

    本地补丁(f2dl)：整批结束后对失败任务自动重试——最多 3 轮、轮间随机
    冷却 10~20 分钟（2026-09-16 已缩短为 60~120 秒），每轮只重跑上一轮
    失败的任务（已成功的不重复下载，已存在文件引擎按存在性跳过）。
    cookie 环境性失败(CookieNotReadyError)仍立即中止整批：环境问题
    （浏览器未关/未登录）重试必然同样失败。
    (Auto-retry rounds for failed tasks after a full batch: up to 3
    rounds, 60-120s apart, re-running only the failures.)
    """
    tasks = []
    for key, plat in PLATFORMS.items():
        directory = plat["conf_dir"]
        if directory.exists():
            tasks.extend((key, p) for p in sorted(directory.glob("*.yaml")))
    if not tasks:
        console.print("[yellow]还没有任何已保存的任务配置。[/yellow]")
        return 0

    pending = list(tasks)
    failed: list = []
    round_no = 0
    try:
        while pending and round_no < 3:
            round_no += 1
            if round_no > 1:
                # 2026-09-16 缩短为 60~120 秒（O2 批次 cookie 复用 + 浏览器
                # 通道后，失败多为瞬时原因，无需小时级等待）。
                cooldown = random.randint(60, 120)
                console.print(
                    f"[dim]重试轮冷却：上一轮 {len(pending)} 个任务失败，"
                    f"{cooldown} 秒后开始第 {round_no}/3 轮（仅重跑失败任务）……[/dim]"
                )
                time.sleep(cooldown)
            console.rule(f"批量追更 · 第 {round_no}/3 轮 · 共 {len(pending)} 个任务")
            failed = []
            for i, (_key, path) in enumerate(pending, 1):
                if i > 1:
                    # 风控冷却：任务间随机间隔，削弱固定节奏的机器特征。
                    # 2026-09-16 缩短为 60~120 秒——post 链路已改走浏览器
                    # 通道（列表/profile 均不经 Argus），长冷却失去前提。
                    cooldown = random.randint(60, 120)
                    console.print(f"[dim]风控冷却：{cooldown} 秒后开始下一个任务……[/dim]")
                    time.sleep(cooldown)
                try:
                    # 本地补丁(O2)：batch_mode=True——批次内复用新鲜 cookie，
                    # 任务结束不清存储，批次结束统一清空（见下方 finally）。
                    # 双模式重跑(2026-09-16)：update_mode 整批统一透传。
                    rc = rerun_task(
                        path,
                        interactive=False,
                        batch_mode=True,
                        update_mode=update_mode,
                    )
                except cookie_mod.CookieNotReadyError as exc:
                    # Q5②：抓取失败是环境性问题（浏览器未关/未登录），后续任务必然
                    # 同样失败，立即中止整批，避免冷却空转；已完成任务不受影响。
                    remaining = len(pending) - i
                    console.print(f"[red]cookie 获取失败，中止整批：{escape(str(exc))}[/red]")
                    if remaining:
                        console.print(
                            f"[yellow]剩余 {remaining} 个任务未尝试。关闭浏览器后重跑 "
                            "[cyan]python dydl.py rerun-all[/cyan] 即可（已完成的不会重复下载）。[/yellow]"
                        )
                    return 1
                except (ValueError, OSError) as exc:
                    console.print(f"[red]任务无法运行：{escape(str(exc))}[/red]")
                    rc = 1
                if rc != 0:
                    failed.append(path)
            pending = failed
    finally:
        # 批次结束（含中止/异常路径）统一清理：内存缓存 + 落盘存储，
        # 维持"cookie 不落盘过夜"的安全生命周期（O2）。
        _BATCH_COOKIE_CACHE.pop("douyin", None)
        cookie_mod.clear_cookie("douyin")
        console.print("[dim]批次结束，已清空 cookie 存储（O2 批次复用收尾）。[/dim]")

    console.rule("批量追更结束")
    if failed:
        console.print(
            f"[red]历经 {round_no} 轮，仍有 {len(failed)}/{len(tasks)} 个任务未成功：[/red]"
        )
        for p in failed:
            console.print(f"  [dim]· {escape(str(p))}[/dim]")
        console.print(
            "[yellow]失败任务已保留 .tmp 断点，稍后可单独重跑补齐。[/yellow]"
        )
        return 1
    console.print(f"[green]全部 {len(tasks)} 个任务完成（共 {round_no} 轮）。[/green]")
    return 0
