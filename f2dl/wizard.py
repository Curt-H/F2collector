"""交互向导：无参数运行时的入口（Q1/Q15/Q16/Q20）。

菜单：1 新任务下载（含高级设置子菜单） 2 重跑已有任务  3 Cookie 管理  q 退出。
"""

from pathlib import Path
from typing import Optional

import click
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from . import confgen, core, console
from . import cookie as cookie_mod
from .apps import BROWSERS, INTERVAL_MODES, PLATFORMS, extract_identifier, get_platform, sanitize_name


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

def _pick_platform(default: str = "douyin") -> str:
    keys = list(PLATFORMS)
    lines = "  ".join(f"{i + 1}.{k}({PLATFORMS[k]['label']})" for i, k in enumerate(keys))
    console.print(f"选择平台: {lines}")
    return click.prompt("平台", type=click.Choice(keys), default=default)


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


def _ask_name(plat_key: str, url: str, mode: str) -> Optional[str]:
    extracted = extract_identifier(plat_key, url) or ""
    fallback = extracted or ("临时配置" if mode == "one" else "需手动输入")
    while True:
        raw = click.prompt(
            f"任务别名（回车={fallback}）", default=extracted, show_default=False
        ).strip()
        if raw:
            try:
                return sanitize_name(raw)
            except ValueError as exc:
                console.print(f"[red]{escape(str(exc))}[/red]")
                continue
        if extracted:
            return extracted
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


def _new_task(browser: str) -> None:
    plat_key = _pick_platform()
    plat = get_platform(plat_key)
    url = click.prompt("请粘贴链接").strip()
    if not url:
        console.print("[yellow]未输入链接，返回主菜单。[/yellow]")
        return
    mode = _choose_mode(plat)
    interval = _ask_interval(mode)
    name = _ask_name(plat_key, url, mode)
    current = confgen.last_values(plat_key, name) if name else {}
    overrides = {}
    if click.confirm("进入高级设置？", default=False):
        overrides = _advanced_menu(plat, current)

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
    plat_key = _pick_platform()
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

