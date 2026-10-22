"""交互向导：无参数运行时的入口（Q1/Q15/Q16/Q20）。

菜单：1 新任务下载（含高级设置子菜单） 2 重跑已有任务  3 Cookie 管理  q 退出。
"""

import threading
import time
from pathlib import Path
from typing import Optional, Tuple

import click
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import confgen, core, console
from . import cookie as cookie_mod
from . import nickname as nickname_mod
from .apps import (
    BROWSERS,
    INTERVAL_MODES,
    PLATFORMS,
    canonical_user_url,
    extract_identifier,
    get_platform,
    sanitize_name,
    validate_url,
)


def run_wizard(browser: str = "firefox") -> None:
    console.print(Panel("F2 下载工具 · 交互向导", style="cyan"))
    try:
        while True:
            console.print(
                "\n[bold]主菜单[/bold]  1.新任务下载  2.重跑已有任务  3.Cookie 管理  4.批量追更  q.退出"
            )
            choice = click.prompt("请选择", type=click.Choice(["1", "2", "3", "4", "q"]), default="1")
            if choice == "1":
                _new_task(browser)
            elif choice == "2":
                _rerun(browser)
            elif choice == "3":
                _cookie_menu(browser)
            elif choice == "4":
                core.rerun_all()
                click.prompt("回车返回主菜单", default="", show_default=False)
            else:
                break
    except (click.Abort, KeyboardInterrupt):
        console.print("\n[dim]已中断。[/dim]")
    console.print("再见。")


# ---------------------------------------------------------------- 新任务

def _choose_mode(plat: dict) -> str:
    modes = plat["modes"]
    if len(modes) == 1:
        console.print(f"模式: {modes[0]}（该平台 v1 仅开放此模式）")
        return modes[0]
    console.print("可选模式: " + "  ".join(modes))
    return click.prompt("模式", type=click.Choice(modes), default="post")


def _ask_interval(mode: str) -> str:
    if mode not in INTERVAL_MODES:
        return "all"
    while True:
        value = click.prompt(
            "日期区间（YYYY-MM-DD|YYYY-MM-DD，回车=all）", default="all", show_default=False
        ).strip()
        try:
            return core.validate_interval(value)
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")


def _ask_name(plat_key: str, url: str, mode: str, nickname: str = "") -> Optional[str]:
    """询问任务别名（Q-NICKNAME）：作者昵称清洗名优先作默认，回车采用/输入更改。"""
    extracted = extract_identifier(plat_key, url) or ""
    default = nickname or extracted
    fallback = default or ("临时配置" if mode == "one" else "需手动输入")
    while True:
        raw = click.prompt(
            f"任务别名（回车={fallback}）", default=default, show_default=False
        ).strip()
        if raw:
            try:
                return sanitize_name(raw)
            except ValueError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                continue
        if default:
            return default
        if mode == "one":
            return None
        console.print("[red]该链接无法自动提取作者标识，请输入一个别名。[/red]")


def _advanced_menu(plat: dict, current: dict) -> dict:
    """高级设置子菜单（Q20-C）：逐项展示当前值，回车保留；只记录用户改动项。"""
    overrides = {}
    console.print("[bold]高级设置[/bold]（回车保留 [当前值]）:")
    for key, kind, label, default in plat["advanced"]:
        current_value = current.get(key, default)
        if kind == "bool":
            value = click.prompt(f"  {label}", default=bool(current_value), type=bool)
        elif kind == "int":
            value = click.prompt(f"  {label}", default=int(current_value), type=int)
        else:
            value = click.prompt(f"  {label}", default=str(current_value)).strip()
        if value != current_value:
            overrides[key] = value
    return overrides


def _handle_duplicate(existing: Path, browser: str) -> None:
    """新任务撞已入库账号（Q-DEDUP-REV，2026-10-13 用户决策）。

    新任务语义 = 只允许新账号：已入库账号报重复、不再静默复用（旧
    Q-DEDUP 行为混淆"新建"与"追更"）；就地询问是否直接追更该任务
    （y = rerun_task 完整追更流程，n = 取消返回）。
    """
    console.print(
        f"[red]重复：该账号已入库为任务「{existing.stem}」，不能重复添加新任务。[/red]"
    )
    if not click.confirm("是否直接追更该任务？", default=True):
        console.print("[yellow]已取消（如需追更请用菜单 2 重跑已有任务）。[/yellow]")
        return
    try:
        rc = core.rerun_task(existing, browser=browser)
    except (ValueError, FileNotFoundError, OSError, cookie_mod.CookieNotReadyError) as exc:
        console.print(f"[red]任务未能运行：{escape(str(exc))}[/red]")
        return
    if rc != 0:
        console.print("[red]任务未成功，可返回菜单重试。[/red]")


def _run_nickname_probe(plat_key: str, url: str) -> None:
    """昵称爬取线程体：结果挂到线程对象上，供 join 后读取。"""
    me = threading.current_thread()
    try:
        raw, cleaned, _source = nickname_mod.resolve_nickname(plat_key, url)
    except Exception:  # noqa: BLE001 - 昵称获取绝不阻塞建任务流程
        raw, cleaned = "", ""
    me.nick_raw, me.nick_clean = str(raw or ""), str(cleaned or "")


def _start_nickname_probe(plat_key: str, url: str) -> threading.Thread:
    """Q-NICKNAME-D3（2026-10-15 共识）：后台线程启动昵称爬取。

    与区间/高级设置等用户交互并行，给爬取（10~60 秒）提供缓冲时间。
    线程必然结束——nickname.resolve_nickname 内部预算兜底（主页通道
    goto 45s + DOM 轮询 15s；one 模式移动 Feed ≤6s）；daemon=True
    保证向导中途退出不挂进程。playwright sync 对象与创建线程强耦合
    （W1 教训）——启动→爬取→关闭全程在本线程内闭环，不跨线程传递。
    """
    thread = threading.Thread(
        target=_run_nickname_probe, args=(plat_key, url), daemon=True
    )
    thread.start()
    return thread


def _await_nickname_probe(thread: threading.Thread) -> Tuple[str, str]:
    """等待后台昵称爬取完成（Q4=A：同步等待就绪，不设额外封顶——线程
    内部预算即天然上限）。等待期间每 5 秒提示一次进度。返回 (raw, clean)。

    线程异常/失败路径不设属性 → ("", "")，由调用方回退 sec_uid 默认（Q5）。
    """
    if thread.is_alive():
        console.print("[dim]昵称爬取尚未完成，等待中……[/dim]")
        waited = 0
        while thread.is_alive():
            thread.join(timeout=1.0)
            waited += 1
            if waited % 5 == 0 and thread.is_alive():
                console.print(f"[dim]昵称爬取中，已等待 {waited}s……[/dim]")
    return getattr(thread, "nick_raw", ""), getattr(thread, "nick_clean", "")


def _new_task(browser: str) -> None:
    # 工具已单平台特化（PLATFORMS 仅注册 douyin），不再询问平台。
    plat_key = "douyin"
    plat = get_platform(plat_key)
    # Q-URLCHECK：粘贴后立即合规校验，非抖音链接（粘错平台/仿冒域名/
    # 纯文本）就地红字提示重粘，合法后才继续——避免等待 cookie 抓取与
    # 浏览器启动后才在 f2 阶段报错。空输入仍返回主菜单。
    while True:
        url = click.prompt("请粘贴链接").strip()
        if not url:
            console.print("[yellow]未输入链接，返回主菜单。[/yellow]")
            return
        try:
            url = validate_url(plat_key, url)
            break
        except ValueError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            console.print("[dim]请重新粘贴（直接回车留空 = 返回主菜单）。[/dim]")
    mode = _choose_mode(plat)
    # Q-DEDUP-REV（2026-10-13 用户决策）：新任务只允许新账号。主页链接先
    # 规范化（剥追踪参数）再提前判重——已入库账号报重复并就地询问是否
    # 直接追更，不再静默复用（旧 Q-DEDUP 行为混淆"新建"与"追更"语义）；
    # core.run_task 有同一判重的兜底（CLI 路径不经过向导）。one 模式走
    # 临时配置不落盘，无需核验；提取不出账号标识（如短链）仅提醒。
    if mode != "one":
        norm = canonical_user_url(plat_key, url)
        if norm:
            _account_key, url = norm
            existing = confgen.find_conf_by_key(plat_key, _account_key)
            if existing:
                _handle_duplicate(existing, browser)
                click.prompt("回车返回主菜单", default="", show_default=False)
                return
        else:
            console.print("[yellow]无法从链接提取账号标识，将不做重复核验。[/yellow]")
    # Q-NICKNAME-D3（2026-10-15 共识）：判重通过（必然新账号）后立即
    # 后台线程启动昵称爬取——与区间/高级设置等用户交互并行，给爬取
    # （10~60 秒）提供缓冲时间；别名询问挪到流程最后（高级设置之后、
    # 确认表之前——Q3），届时同步等待爬取就绪（Q4=A）；拿不到昵称 →
    # 静默回退 sec_uid 标识默认（Q5）。CLI 路径保持串行现状（Q6）。
    nick_probe = _start_nickname_probe(plat_key, url)
    interval = _ask_interval(mode)
    overrides = {}
    if click.confirm("进入高级设置？", default=False):
        # F4：Q-DEDUP-REV 后走到这里的必然是新账号，last_values 恒空
        # ——高级设置用官方默认，与后置的别名无数据依赖。
        overrides = _advanced_menu(plat, {})
    raw_nick, nick_clean = _await_nickname_probe(nick_probe)
    if nick_clean:
        console.print(f"[green]已获取作者昵称：{escape(raw_nick)}[/green]")
        if nick_clean != raw_nick:
            console.print(
                f"[dim]昵称含特殊字符/emoji，清洗后作为默认别名：{escape(nick_clean)}[/dim]"
            )
    else:
        console.print(
            "[dim]未能从用户主页获取昵称，别名可手动输入，或回车用链接标识。[/dim]"
        )
    name = _ask_name(plat_key, url, mode, nickname=nick_clean)

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_row("平台", f"{plat['label']} ({plat_key})")
    table.add_row("链接", escape(url))
    table.add_row("模式", mode)
    table.add_row("区间", interval)
    table.add_row("别名", "(临时配置)" if mode == "one" else name or "(自动标识)")
    table.add_row("自定义项", ", ".join(overrides) if overrides else "无")
    console.print(table)

    if not click.confirm("确认生成配置并下载？", default=True):
        console.print("[yellow]已取消。[/yellow]")
        return
    try:
        rc = core.run_task(
            plat_key, url, mode,
            name=name, interval=interval, browser=browser, overrides=overrides,
        )
    except (ValueError, cookie_mod.CookieNotReadyError) as exc:
        console.print(f"[red]错误：{escape(str(exc))}[/red]")
        rc = 1
    if rc != 0:
        console.print("[red]任务未成功，可返回菜单重试。[/red]")
    click.prompt("回车返回主菜单", default="", show_default=False)


# ---------------------------------------------------------------- 重跑

def _rerun(browser: str = "firefox") -> None:
    tasks = []
    for key, plat in PLATFORMS.items():
        directory = plat["conf_dir"]
        if directory.exists():
            tasks.extend((key, p) for p in sorted(directory.glob("*.yaml")))
    if not tasks:
        console.print("[yellow]还没有任何已保存的任务配置（跑一个新任务后会出现在这里）。[/yellow]")
        return
    table = Table(title="已有任务")
    table.add_column("编号", justify="right")
    table.add_column("平台")
    table.add_column("任务")
    table.add_column("模式")
    table.add_column("上次运行")
    table.add_column("链接")
    for i, (key, path) in enumerate(tasks, 1):
        data = confgen.load_conf(path)
        section = next(iter(data.values())) if data else {}
        table.add_row(
            str(i), PLATFORMS[key]["label"], path.stem,
            str(section.get("mode", "")),
            str(section.get("last_run", "从未")),
            escape(str(section.get("url", ""))[:40]),
        )
    console.print(table)
    sel = click.prompt("选择任务编号（0 取消）", type=int, default=0)
    if not 1 <= sel <= len(tasks):
        console.print("[yellow]已取消。[/yellow]")
        return
    try:
        core.rerun_task(tasks[sel - 1][1], browser=browser)
    except (ValueError, FileNotFoundError, OSError, cookie_mod.CookieNotReadyError) as exc:
        console.print(f"[red]任务未能运行：{escape(str(exc))}[/red]")


# ---------------------------------------------------------------- Cookie

def _cookie_menu(browser: str) -> None:
    # 工具已单平台特化（PLATFORMS 仅注册 douyin），不再询问平台。
    plat_key = "douyin"
    plat = get_platform(plat_key)
    current = cookie_mod.load_cookie(plat_key)
    console.print(f"当前 cookie: {cookie_mod.mask(current)}")
    console.print(f"存放位置: {plat['cookie_file']}")
    console.print(
        "[dim]任务每次启动都会强制重抓并覆盖此处 cookie；本菜单仅用于手动诊断/救急。[/dim]"
    )
    choice = click.prompt(
        "1.浏览器抓取  2.手动粘贴  3.从f2配置导入  b.返回",
        type=click.Choice(["1", "2", "3", "b"]), default="b",
    )
    if choice == "b":
        return
    if choice == "3":
        ok, saved, source = cookie_mod.import_cookie_from_f2(plat_key)
        if ok:
            console.print(
                f"[green]已从 {source} 导入: {cookie_mod.mask(saved)}[/green]"
            )
        else:
            console.print("[red]本机 f2 包内配置(app.yaml)中没有该平台的 cookie。[/red]")
        return
    if choice == "1":
        grab_browser = click.prompt("浏览器", type=click.Choice(BROWSERS), default=browser)
        console.print(f"[yellow]请先完全关闭 {grab_browser}，然后回车继续……[/yellow]")
        click.prompt("回车继续", default="", show_default=False)
        ok, grabbed = cookie_mod.grab_cookie(plat_key, grab_browser)
        if ok:
            console.print(f"[green]抓取成功: {cookie_mod.mask(grabbed)}[/green]")
            return
        console.print("[red]抓取失败（常见原因：浏览器未关闭/未登录抖音或微博/权限不足）。[/red]")
        choice = "2"  # Q9：自动回退为手动粘贴
    raw = click.prompt("粘贴 cookie（回车放弃）", default="", show_default=False)
    if not raw.strip():
        console.print("[yellow]已放弃。[/yellow]")
        return
    try:
        saved = cookie_mod.paste_cookie(plat_key, raw)
    except ValueError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        return
    console.print(f"[green]已保存: {cookie_mod.mask(saved)}[/green]")

