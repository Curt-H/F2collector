"""命令行入口：有任务参数直接生成配置并下载，无参数进入交互向导（Q1-C）。"""

import sys

import click

from . import console, core, wizard
from . import cookie as cookie_mod
from .apps import BROWSERS, PLATFORMS


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option("-p", "--platform", default="douyin", type=click.Choice(list(PLATFORMS)),
    help="平台（工具已单平台特化，默认 douyin，通常无需指定）")
@click.option("-u", "--url", help="链接：单作品链接或主页链接（配合 -M）")
@click.option(
    "-M", "--mode",
    help="下载模式：one/post/like/collection/collects/music/mix/live/related/friend",
)
@click.option("-n", "--name", "task_name", help="任务别名（配置文件名），缺省自动从链接提取")
@click.option(
    "-i", "--interval", default="all", show_default=True,
    help="下载日期区间 YYYY-MM-DD|YYYY-MM-DD，或 all",
)
@click.option(
    "-b", "--browser", default="firefox", show_default=True,
    type=click.Choice(BROWSERS), help="cookie 抓取所用浏览器（Firefox 的 cookie 明文存储，最稳）",
)
@click.option("--gen-only", is_flag=True, default=False, help="仅生成任务配置文件，不启动下载")
@click.pass_context
def main(ctx, platform, url, mode, task_name, interval, browser, gen_only):
    """F2 下载工具：自动生成 f2 配置并调用 f2 下载（douyin）。

    不带任何参数运行 = 进入交互向导。
    """
    if ctx.invoked_subcommand is not None:
        return
    if not url:
        wizard.run_wizard(browser=browser)  # Q1=C：无参数进向导
        return
    if not mode:
        raise click.UsageError(
            "命令行下载需要 -u 链接、-M 模式 两项齐备（或都不带参数进入向导）"
        )
    try:
        rc = core.run_task(
            platform, url, mode,
            name=task_name, interval=interval, browser=browser, gen_only=gen_only,
        )
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        console.print(f"[red]错误：{escape_msg(exc)}[/red]")
        sys.exit(2)
    except KeyboardInterrupt:
        console.print("\n[dim]已取消。[/dim]")
        sys.exit(130)
    sys.exit(rc)


def escape_msg(exc: Exception) -> str:
    from rich.markup import escape

    return escape(str(exc))


@main.command("rerun-all")
def rerun_all_cmd():
    """批量追更全部任务：任务间随机冷却，单个失败不中断，失败任务自动重试。

    本地补丁(2026-09-16, 双模式重跑)：交互终端下批次开始前询问重跑方式
    （增量/全量，回车默认增量），整批统一；非交互（脚本/计划任务）默认
    增量，保持自动化兼容。
    """
    update_mode = None
    if sys.stdin.isatty():
        choice = click.prompt(
            "批量重跑方式：1.增量更新（自各任务上次运行起，回车默认）"
            "  2.全量更新（扫描全部作品，已下载自动跳过）",
            type=click.Choice(["1", "2"]),
            default="1",
            show_default=False,
        )
        update_mode = "incremental" if choice == "1" else "full"
    sys.exit(core.rerun_all(update_mode=update_mode))


@main.command("cookie")
@click.option("-p", "--platform", default="douyin", type=click.Choice(list(PLATFORMS)),
    help="平台（默认 douyin，通常无需指定）")
@click.option("-b", "--browser", default="firefox", show_default=True,
    type=click.Choice(BROWSERS), help="cookie 抓取所用浏览器（Firefox 的 cookie 明文存储，最稳）")
@click.option("--paste", "paste_flag", is_flag=True, default=False, help="手动粘贴 cookie（否则浏览器抓取）")
@click.option(
    "--import-f2", "import_f2", is_flag=True, default=False,
    help="从本机 f2 包内配置(app.yaml)导入之前用 `f2 --auto-cookie` 抓过的 cookie，无需重新抓取",
)
def cookie_cmd(platform, browser, paste_flag, import_f2):
    """手动抓取/粘贴 cookie 并保存（仅诊断与救急用：任务每次启动都会强制重抓）。"""
    plat = PLATFORMS[platform]
    if import_f2:
        ok, saved, source = cookie_mod.import_cookie_from_f2(platform)
        if ok:
            console.print(
                f"[green]已从 {source} 导入 {plat['label']} cookie，"
                f"保存到 {plat['cookie_file']}: {cookie_mod.mask(saved)}[/green]"
            )
        else:
            console.print(
                "[red]本机 f2 包内配置(app.yaml)中没有该平台的 cookie；"
                "请用浏览器抓取（默认）或 --paste 手动粘贴。[/red]"
            )
            sys.exit(1)
        return
    if paste_flag:
        raw = click.prompt("粘贴 cookie")
        try:
            saved = cookie_mod.paste_cookie(platform, raw)
        except ValueError as exc:
            console.print(f"[red]错误：{escape_msg(exc)}[/red]")
            sys.exit(2)
        console.print(f"[green]已保存到 {plat['cookie_file']}: {cookie_mod.mask(saved)}[/green]")
        return
    console.print(f"尝试从 {browser} 抓取 {plat['label']} cookie（请确保已关闭该浏览器）……")
    ok, saved = cookie_mod.grab_cookie(platform, browser)
    if ok:
        console.print(f"[green]成功，已保存到 {plat['cookie_file']}: {cookie_mod.mask(saved)}[/green]")
    else:
        console.print("[red]抓取失败：请关闭对应浏览器后重试，或加 --paste 手动粘贴。[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
