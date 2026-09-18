# path: f2/apps/douyin/browser_fetch.py

"""本地补丁(f2dl)：Firefox 真实浏览器通道(用户作品列表,post 主通道)。

背景:Web 端作品列表接口 `/aweme/v1/web/aweme/post/` 前置 Argus
SecurityPlugin 风控网关,对无完整设备凭证的 httpx 请求按概率 403
("Uifid Not Found"),UIFID header 与纯 Python a_bogus 签名均无法
真正通过设备凭证校验(见 DouyinCrawler.__init__ 补丁注释);
2026-09-15 风控收紧窗口内实测 8/8 全 403。

本通道思路:与已删除的 msToken 保活实验(token_refresh.py,"访问首页等
Set-Cookie 颁发 msToken",2026-09-11 实测 6 种组合均不颁发,2026-09-17
随死代码清理删除)本质不同——
用 Playwright 驱动 Firefox 打开用户主页,由页面自身 JS 在真实浏览器
环境(TLS/HTTP2/指纹全真)现场生成 msToken/a_bogus 并发起列表请求,
Python 仅旁路拦截响应(page.on("response")),完全不参与签名与请求
构造。P0 探针(scripts/probe_browser_post.py)实证:同一风控窗口内无头/有头
4/4 全 200,scripts/verify_browser_filter.py 验证拦截 JSON 可零改动通过
UserPostFilter 解析。

全自动滚动语义(2026-09-16 共识):
- 每次滚动后随机 CD 5~15 秒,期间每 0.5 秒检查新页(到达即提前结束
  等待);CD 走完仍无新页再额外等待 20 秒;
- 单轮滚动无新页 → 轮换滚动姿势(滚轮→scrollTo→End 键)且观察期递增
  (额外等待 20s→40s→60s),连续 3 轮无新页才会话终止;
- 会话异常终止 → 自动开新会话续滚(从最新重滚、until_cursor 定位
  深度、作品按 aweme_id 去重),最多 5 次,会话间随机冷却 5~10 秒;
- 停止条件(任一):真实 has_more=False / 逐条命中 min_cursor(增量
  窗口覆盖,页内最旧作品早于阈值即停) / 总滚动时长预算(30 分钟)耗尽 /
  续滚次数上限;
- 逐条剔除:聚合时剔除 create_time ≤ min_cursor 的作品(增量窗口外
  的旧作品不进下载管线;min_cursor 为毫秒时间戳,由 handle_user_post
  按双轨语义写入 kwargs——interval=all 用 last_run 时刻,显式区间用
  区间起点,皆无=0 全量)。

使用方式:DouyinCrawler.fetch_user_post 的唯一通道(直接首选);批量
重跑由 rerun_all 开头统一询问增量/全量后整批传入。2026-09-16 修订:
取消 httpx 兜底——收紧窗口内 httpx 8 次紧凑重试必然全 403,徒增 IP
风控暴露;通道失败直接抛 APIError 走 handler 既有风控路径(SystemExit(1)
干净退出,f2dl 编排层冷却重跑),绝不静默返回空数据。
"""


import asyncio
import random
import re
import time
from typing import Dict, List, Optional, Tuple

from f2.exceptions.api_exceptions import APIError
from f2.log.logger import logger
from f2.i18n.translator import _

#: 拦截的作品列表接口 path(Argus 门禁接口,由页面 JS 现场签名发起)
_POST_PATH_MARK = "/aweme/v1/web/aweme/post/"

#: 滚动后的随机 CD 范围(秒)——防风控节奏;期间每 0.5s 检查新页到达
_SCROLL_CD_RANGE_SEC = (5, 15)

#: 新页轮询间隔(秒)
_POLL_INTERVAL_SEC = 0.5

#: CD 走完仍无新页时的额外等待(秒)
_NEWPAGE_EXTRA_WAIT_SEC = 20

#: 连续滚动失败轮的递增观察期(秒)——第 2/3 轮在 CD+20s 基础上再叠加
_SCROLL_RETRY_EXTRA = (20, 40, 60)

#: 连续滚动无新页的轮数上限(达到即会话终止→续滚)
_MAX_SCROLL_FAIL_ROUNDS = 3

#: 自动续滚(重开会话)次数上限
_MAX_RENEW_SESSIONS = 5

#: 续滚会话之间的随机冷却范围(秒)
_RENEW_COOLDOWN_RANGE_SEC = (5, 10)

#: 单任务总滚动时长预算(秒)——防 cookie 失效空转/超长全量失控;
#: 达限按"部分完成"正常结束(已拉作品照常下载,日志明示)
_TOTAL_SCROLL_BUDGET_SEC = 30 * 60

#: 页面加载超时(毫秒)——2026-09-16 从 30s 放宽到 90s(网络慢/收紧限流下
#: domcontentloaded 也可能超过 30s,加载太慢会被误判为失败)
_GOTO_TIMEOUT_MS = 90_000

#: 等待首屏列表响应的超时(秒)——2026-09-16 从 30s 放宽到 60s(同上)
_FIRST_PAGE_TIMEOUT_SEC = 60

#: 有头模式人工登录等待上限(秒)——检测到登录墙时给用户扫码时间
_MANUAL_LOGIN_WAIT_SEC = 180

#: 会话终点缓存 {sec_user_id: 上次聚合返回的 max_cursor}。
#  handler 翻页循环会带着上一页返回的 max_cursor 再次调用 fetch_user_post;
#  浏览器通道是"一次拉完"的任务式聚合,再次调用时直接以空列表干净终止,
#  防止 handler 循环重复起浏览器滚动。
_SESSION_END_CURSOR: Dict[str, int] = {}


def _has_login_cookie(cookie: str) -> bool:
    """cookie header 是否含抖音登录态核心 cookie(sessionid_ss/sessionid)。"""
    for part in (cookie or "").split(";"):
        name = part.strip().partition("=")[0]
        if name in ("sessionid_ss", "sessionid"):
            return True
    return False


def _detect_login_wall(page) -> bool:
    """页面是否呈现登录墙特征(扫码登录/点击登录等文案)。"""
    try:
        text = page.evaluate(
            "() => document.body ? (document.body.innerText || '') : ''"
        ) or ""
    except Exception:  # noqa: BLE001 - 页面跳转中偶发执行失败按未检出处理
        return False
    return bool(re.search(r"扫码登录|点击登录|登录抖音|验证码登录", text))


def _parse_cookie_header(header: str) -> List[dict]:
    """cookie header → Playwright add_cookies 数组(仅 .douyin.com)。"""
    cookies = []
    for part in (header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name and value:
            cookies.append(
                {"name": name, "value": value, "domain": ".douyin.com", "path": "/"}
            )
    return cookies


def _do_scroll(page, posture_idx: int) -> None:
    """执行一次滚动（2026-09-16 滚动诊断探针实测校准）。

    诊断结论：抖音 web 用户页的 window 滚动层不可滚
    （scrollingElement.scrollHeight==视口高），列表由 wheel 事件驱动
    （虚拟滚动）——wheel@视口内容区中心是唯一稳定推进游标的姿势，
    渐进式多步滚轮保持事件流；window.scrollTo/End 键作为辅助轮换。
    """
    try:
        posture = posture_idx % 3
        if posture == 0:
            # 主力:鼠标置于视口内容区中心,渐进式滚轮(保持滚动事件流)
            page.mouse.move(960, 540)
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(150)
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(150)
            page.mouse.wheel(0, 1600)
        elif posture == 1:
            page.evaluate("window.scrollBy(0, 1200)")
            page.wait_for_timeout(120)
            page.mouse.move(960, 540)
            page.mouse.wheel(0, 2800)
        else:
            page.keyboard.press("End")
            page.wait_for_timeout(120)
            page.mouse.move(960, 540)
            page.mouse.wheel(0, 2000)
    except Exception:  # noqa: BLE001 - 单次滚动失败由重试机制兜底
        pass


def _observe_new_page(page, before: int, pages: List[dict], total_wait: float) -> bool:
    """在 total_wait 秒内每 0.5s 检查一次新页;到达即提前返回 True。"""
    deadline = time.time() + total_wait
    while time.time() < deadline:
        if len(pages) > before:
            return True
        try:
            page.wait_for_timeout(int(_POLL_INTERVAL_SEC * 1000))
        except Exception:  # noqa: BLE001
            break
    return len(pages) > before


#: 作品总数 DOM 探测超时(秒)——首屏就绪后页面 tab 计数可能滞后渲染
_TOTAL_PROBE_TIMEOUT_SEC = 15

#: 全量预算公式：每页(约 20 条)预算 45 秒，×2 保险系数(重叠页/续滚重滚)
_FULL_BUDGET_SEC_PER_PAGE = 45 * 2
#: 全量续滚上限公式：每 2 页估计缺口允许 1 次续滚
_RENEW_PER_PAGES = 2


def _extract_total_from_dom(page) -> Optional[int]:
    """从用户主页 DOM 提取作品总数（"作品 N"标签，即用户截图位置）。

    页面 tab 计数可能滞后于首屏渲染，轮询最多 _TOTAL_PROBE_TIMEOUT_SEC；
    提取失败（页面改版/私密账号无计数）返回 None——调用方降级为现有行为
    （无总数校验，靠续滚+预算），日志明示。

    (Extract the total works count from the profile page DOM, e.g. the
    "作品 682" tab label.)
    """
    import re as _re

    deadline = time.time() + _TOTAL_PROBE_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            body_text = page.evaluate("() => document.body.innerText") or ""
        except Exception:  # noqa: BLE001 - 页面跳转中偶发执行失败，重试
            body_text = ""
        m = _re.search(r"作品\s*(\d+)", body_text)
        if m:
            return int(m.group(1))
        try:
            page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            break
    return None


def _collect_pages_sync(
    cookie: str,
    sec_user_id: str,
    until_cursor: int,
    min_cursor: int = 0,
    headless: bool = True,
    deadline: float = 0.0,
) -> Dict:
    """同步阻塞:Firefox 会话打开用户主页,滚动翻页收集原始响应页。

    停止条件(任一满足):真实 has_more=False / 覆盖到 until_cursor 深度 /
    页内最旧作品早于 min_cursor(增量窗口已覆盖) / 滚动无新页 /
    总滚动预算(deadline)耗尽。

    注:曾尝试进程内常驻 playwright 实例跨会话复用(W1),因 playwright
    sync 对象与创建线程强耦合而回退为每会话独立 start/stop。

    Returns:
        dict: {"pages": [页记录...], "end_reason": str}
        页记录: {"aweme_list", "max_cursor", "has_more", "oldest_ct"}
        end_reason: "has_more" / "until_cursor" / "min_cursor" /
                    "scroll_fail" / "budget"
    """
    user_url = f"https://www.douyin.com/user/{sec_user_id}"
    pages: List[dict] = []
    seen_ids: set = set()
    end_reason = "scroll_fail"
    fail_rounds = 0
    scroll_idx = 0
    # 已收作品的最旧 create_time(秒)——Fix E：重叠响应页(fresh=[])会产出
    # oldest_ct=0，页级判定被 falsy 遮蔽；改用会话级累计值判定，空页不回退
    min_seen_ct = 0

    from playwright.sync_api import sync_playwright

    # 每次会话独立 start/stop——playwright sync 对象绑定创建线程(W1 备注),
    # 进程内常驻复用不可行;单会话内保持实例,结束后完整释放。
    pw = sync_playwright().start()
    browser = pw.firefox.launch(headless=headless)
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="zh-CN",
        # 不覆盖 User-Agent:保持 Playwright Firefox 自身真实指纹
    )
    try:
        # 基础反检测:抹掉 navigator.webdriver
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', "
            "{get: () => undefined})"
        )
        cookies = _parse_cookie_header(cookie)
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()

        def on_response(resp):
            nonlocal min_seen_ct  # Fix E：嵌套函数内赋值需 nonlocal 声明
            if _POST_PATH_MARK not in resp.url:
                return
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001 - 非 JSON 响应直接忽略
                return
            fresh = []
            for item in data.get("aweme_list") or []:
                aweme_id = str(item.get("aweme_id") or "")
                if aweme_id and aweme_id not in seen_ids:
                    seen_ids.add(aweme_id)
                    fresh.append(item)
            fresh_ct = min(
                (int(a.get("create_time") or 0) for a in fresh),
                default=0,
            )
            # Fix E：会话级最旧时间——重叠空页(fresh=[])不回退判定值
            if fresh_ct and (min_seen_ct == 0 or fresh_ct < min_seen_ct):
                min_seen_ct = fresh_ct
            pages.append(
                {
                    "aweme_list": fresh,
                    "max_cursor": data.get("max_cursor"),
                    "has_more": data.get("has_more"),
                    # 页内最旧作品创建时间(秒级)
                    "oldest_ct": fresh_ct,
                }
            )

        page.on("response", on_response)
        logger.info(
            _("浏览器会话已启动({0})，打开用户主页等待首屏列表……").format(
                _("无头") if headless else _("有头")
            )
        )
        page.goto(
            user_url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS
        )

        # 登录状态检测(2026-09-16 用户需求)：无头/有头会话都检测并打日志；
        # 有头模式检测到登录墙时给 180 秒人工扫码窗口(救场路径)。
        mode_txt = _("无头") if headless else _("有头")
        has_login_cookie = _has_login_cookie(cookie)
        login_wall = _detect_login_wall(page)
        if login_wall:
            logger.warning(
                _("登录状态检测({0})：页面呈现登录墙——cookie 可能已过期或未登录").format(
                    mode_txt
                )
            )
        elif has_login_cookie:
            logger.info(_("登录状态检测({0})：已登录").format(mode_txt))
        else:
            logger.warning(
                _("登录状态检测({0})：cookie 中无登录态(sessionid 缺失)——可能未登录").format(
                    mode_txt
                )
            )

        if login_wall and not headless:
            # 有头人工登录救场:等待扫码完成(最长 180 秒)
            logger.warning(
                _("请在弹出的 Firefox 窗口中登录抖音账号（最长等待 {0} 秒）……").format(
                    _MANUAL_LOGIN_WAIT_SEC
                )
            )
            wait_deadline = time.time() + _MANUAL_LOGIN_WAIT_SEC
            while time.time() < wait_deadline:
                page.wait_for_timeout(2000)
                if not _detect_login_wall(page):
                    login_wall = False
                    logger.info(_("检测到登录成功，继续拉取……"))
                    break
            if login_wall:
                raise APIError(
                    _("有头模式人工登录等待超时({0} 秒)，仍未登录").format(
                        _MANUAL_LOGIN_WAIT_SEC
                    )
                )

        # 等待首屏列表响应
        deadline_wait = time.time() + _FIRST_PAGE_TIMEOUT_SEC
        while time.time() < deadline_wait and not pages:
            page.wait_for_timeout(500)
        if not pages:
            state_txt = _("已登录") if has_login_cookie else _("无登录态 cookie")
            wall_txt = _("；页面呈现登录墙") if login_wall else ""
            raise APIError(
                "浏览器通道未捕获到作品列表响应"
                f"(登录状态:{state_txt}{wall_txt};网络/限流也可能),"
                "请更新 cookie 或稍后重试"
            )

        logger.info(
            _(
                "首屏就绪：1 页 / {0} 条作品，开始滚动翻页"
                "（每页随机 CD {1}~{2} 秒）……"
            ).format(
                len(pages[0].get("aweme_list") or []),
                int(_SCROLL_CD_RANGE_SEC[0]),
                int(_SCROLL_CD_RANGE_SEC[1]),
            )
        )

        # 作品总数 DOM 探测(2026-09-16 全量核对共识)：主页"作品 N"标签。
        # 失败(改版/私密账号)返回 None → 无总数校验，靠续滚+预算。
        total = _extract_total_from_dom(page)
        if total:
            logger.info(_("该账号作品总数：{0} 条（主页计数）").format(total))
        else:
            logger.info(_("未能从主页获取作品总数（将依赖 has_more 判定）"))

        # 滚动翻页主循环:CD 5~15s 合并新页观察;失败轮换姿势+递增等待
        while True:
            last = pages[-1]
            if last.get("has_more") is False:
                end_reason = "has_more"
                break
            # 全量核对(2026-09-16)：已收去重条数达到主页计数 → 完美完成
            if total and len(seen_ids) >= total:
                end_reason = "total_reached"
                break
            if (
                until_cursor
                and last.get("max_cursor") is not None
                and int(last["max_cursor"]) <= int(until_cursor)
            ):
                end_reason = "until_cursor"
                break  # 已覆盖到调用方起始 cursor 深度
            if (
                min_cursor
                and min_seen_ct
                and min_seen_ct * 1000 <= int(min_cursor)
            ):
                end_reason = "min_cursor"
                break  # 增量窗口已覆盖:已收作品最旧者早于 min_cursor(Fix E)
            if deadline and time.time() > deadline:
                end_reason = "budget"
                break  # 总滚动预算耗尽

            _do_scroll(page, scroll_idx)
            scroll_idx += 1
            # 观察期 = 随机 CD + 20s 基础额外等待 + 失败轮递增(0/20/40)
            total_wait = random.uniform(*_SCROLL_CD_RANGE_SEC)
            total_wait += _NEWPAGE_EXTRA_WAIT_SEC
            if fail_rounds:
                total_wait += _SCROLL_RETRY_EXTRA[
                    min(fail_rounds - 1, len(_SCROLL_RETRY_EXTRA) - 1)
                ]
            # 进度可观测(2026-09-16 方案2)：会话内逐次滚动打点，
            # 消除分钟级静默造成的"卡死"体感
            logger.info(
                _(
                    "第 {0} 次滚动（已收 {1} 页 / {2} 条），"
                    "随机 CD 后观察新页（约 {3:.0f} 秒）……"
                ).format(
                    scroll_idx,
                    len(pages),
                    sum(len(pg.get("aweme_list") or []) for pg in pages),
                    total_wait,
                )
            )
            if _observe_new_page(page, len(pages), pages, total_wait):
                fail_rounds = 0  # 新页到达,恢复满耐心
            else:
                fail_rounds += 1
                logger.info(
                    _(
                        "第 {0} 轮滚动未触发新页"
                        "（连续 {1}/{2} 轮），换姿势递增等待重试……"
                    ).format(fail_rounds, fail_rounds, _MAX_SCROLL_FAIL_ROUNDS)
                )
                if fail_rounds >= _MAX_SCROLL_FAIL_ROUNDS:
                    end_reason = "scroll_fail"
                    break  # 会话终止,由续滚层决定是否重开会话
    finally:
        # 完整释放本会话资源(context/browser/driver)
        try:
            context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass
    return {"pages": pages, "end_reason": end_reason, "total": total}


async def _run_session(
    cookie: str,
    sec_user_id: str,
    until_cursor: int,
    min_cursor: int,
    deadline: float,
) -> Tuple[Dict, bool]:
    """单会话:无头首选,APIError 时自动有头重试一次。

    W1 备注:曾尝试进程内常驻 playwright 实例跨会话复用,但 playwright
    sync 对象绑定创建线程(sync 内部 greenlet/loop 与线程强耦合),跨线程
    使用缓存实例会触发 "Sync API inside the asyncio loop"——已回退为
    每次会话独立 start/stop(增量收益让位于可靠性)。

    Returns:
        (会话结果 dict, 是否全程无头)
    """
    try:
        # sync playwright 放工作线程执行,不阻塞 f2 的 asyncio 事件循环
        return (
            await asyncio.to_thread(
                _collect_pages_sync,
                cookie,
                sec_user_id,
                until_cursor,
                min_cursor,
                True,
                deadline,
            ),
            True,
        )
    except APIError as headless_exc:
        logger.warning(
            _(
                "无头浏览器通道失败({0})，改用有头模式重试"
                "（会短暂弹出 Firefox 窗口）……"
            ).format(headless_exc)
        )
        return (
            await asyncio.to_thread(
                _collect_pages_sync,
                cookie,
                sec_user_id,
                until_cursor,
                min_cursor,
                False,
                deadline,
            ),
            False,
        )


async def fetch_user_post_via_browser(
    cookie: str,
    sec_user_id: str,
    max_cursor: int = 0,
    min_cursor: int = 0,
) -> Dict:
    """Firefox 真实浏览器通道获取用户作品列表(与 fetch_user_post 返回同构)。

    内部编排多会话自动续滚:会话因滚动连续失败异常终止时,自动重开会话
    续滚(从最新重滚、until_cursor 定位深度、跨会话按 aweme_id 去重)。

    本地补丁(2026-09-16, 全量核对)：会话内从主页 DOM"作品 N"标签提取
    作品总数——已知后动态扩展总预算（页数×45s×2，682 条 ≈ 53 分钟）与
    续滚上限（每 2 页缺口 1 次），停止条件增加"已收 ≥ 总数"；结束时
    全量核对已收与主页计数，缺口（可能含私密/仅自己可见作品）醒目报告，
    任务按部分完成处理。总数提取失败（页面改版/私密账号）降级为现有
    续滚+预算行为。

    Args:
        cookie: 登录 cookie header(来自 DouyinCrawler.headers)
        sec_user_id: 用户 ID
        max_cursor: 调用方起始 cursor(翻页深度覆盖到此为止;0=从最新开始)
        min_cursor: 增量窗口下限(毫秒;逐条剔除 create_time ≤ 阈值的作品
                    并停止翻页;0=全量,滚到 has_more=False)

    Returns:
        dict: {"aweme_list": [...], "max_cursor": int, "has_more": bool,
               "status_code": 0} —— 可直接交 UserPostFilter 解析。

    Raises:
        APIError: 首会话无头+有头均失败(playwright 未安装/被拦截等)。
    """
    # handler 翻页循环带着上次聚合终点 cursor 回来:干净终止,防重复滚动
    last_end = _SESSION_END_CURSOR.get(sec_user_id)
    if last_end is not None and int(max_cursor or 0) == int(last_end):
        logger.info(_("浏览器通道:已到达本次会话终点,无更多作品"))
        return {
            "aweme_list": [],
            "max_cursor": int(max_cursor or 0),
            "has_more": False,
            "status_code": 0,
        }

    logger.info(
        _(
            "作品列表走 Firefox 浏览器通道(Playwright,页面现场签名,"
            "Python 仅拦截响应;增量窗口下限 {0})……"
        ).format(min_cursor or 0)
    )

    start = time.time()
    budget_sec = _TOTAL_SCROLL_BUDGET_SEC
    deadline = start + budget_sec
    all_pages: List[dict] = []
    seen_ids: set = set()
    until = int(max_cursor or 0)
    sessions = 0
    used_headless = True
    stop_reason = ""
    total: Optional[int] = None
    max_sessions = _MAX_RENEW_SESSIONS

    try:
        while True:
            if sessions >= max_sessions:
                stop_reason = _("续滚次数上限({0} 次)").format(max_sessions)
                break
            if time.time() > deadline:
                stop_reason = _("总滚动时长预算({0} 分钟)耗尽").format(
                    budget_sec // 60
                )
                break
            if sessions:
                cooldown = random.uniform(*_RENEW_COOLDOWN_RANGE_SEC)
                logger.info(
                    _(
                        "会话滚动连续失败，{0:.0f} 秒后自动续滚"
                        "（第 {1}/{2} 次，从最新重滚并去重）……"
                    ).format(cooldown, sessions, max_sessions)
                )
                await asyncio.sleep(cooldown)
            sessions += 1

            result, was_headless = await _run_session(
                cookie or "", sec_user_id, until, int(min_cursor or 0), deadline
            )
            used_headless = used_headless and was_headless
            pages = result.get("pages") or []
            reason = result.get("end_reason") or "scroll_fail"
            if pages:
                all_pages.extend(pages)
            logger.info(
                _("第 {0} 次会话完成:{1} 页,终止原因={2}").format(
                    sessions, len(pages), reason
                )
            )

            # 主页作品总数(DOM 提取)——首次得知后按总数动态扩展预算与续滚上限
            # （全量核对共识：预算=页数×45s×2；续滚上限=每 2 页缺口 1 次）
            session_total = result.get("total")
            if session_total and session_total != total:
                total = session_total
                est_pages = (total + 19) // 20
                budget_sec = max(
                    _TOTAL_SCROLL_BUDGET_SEC,
                    est_pages * _FULL_BUDGET_SEC_PER_PAGE,
                )
                deadline = max(deadline, start + budget_sec)
                max_sessions = max(
                    _MAX_RENEW_SESSIONS,
                    est_pages // _RENEW_PER_PAGES + 1,
                )
                logger.info(
                    _(
                        "按作品总数 {0} 条动态调整：总预算 {1:.0f} 分钟，"
                        "续滚上限 {2} 次"
                    ).format(total, budget_sec / 60, max_sessions)
                )

            # 已收去重条数（跨会话聚合口径）
            collected = len(
                {
                    str(a.get("aweme_id"))
                    for pg in all_pages
                    for a in pg.get("aweme_list") or []
                }
            )

            # ── 停止判定（三态缺口模型，2026-09-16 共识）──
            if total and collected >= total:
                stop_reason = "total_reached"
                break  # 已收条数达到主页计数：完美完成
            if reason in ("total_reached", "has_more", "min_cursor", "until_cursor"):
                stop_reason = reason
                break  # has_more=服务端确认无更多公开作品（缺口可能含私密作品）
            if reason == "budget":
                stop_reason = "budget"
                break  # 预算是硬保险丝：耗尽即部分完成（尾部缺口报告）

            # scroll_fail（或续滚后仍 scroll_fail）→ 自动续滚加码：
            # 下会话快速定位到本会话深度（until_cursor），total 已知时
            # 预算与续滚上限均已动态扩展
            last_cursor = pages[-1].get("max_cursor") if pages else None
            if last_cursor is not None:
                until = int(last_cursor)
    except ImportError:
        raise APIError(
            "浏览器通道需要 playwright:"
            "pip install playwright && playwright install firefox"
        )
    except APIError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise APIError(_("浏览器通道异常:{0}").format(exc)) from exc

    # 跨会话聚合(按 aweme_id 去重——续滚会话从最新重滚,重复页不重复计)
    # + 逐条剔除(增量窗口外的旧作品不进下载管线)
    cutoff_sec = int(min_cursor or 0) // 1000
    aweme_list: List[dict] = []
    removed = 0
    seen: set = set()
    for pg in all_pages:
        for item in pg.get("aweme_list") or []:
            aweme_id = str(item.get("aweme_id") or "")
            if aweme_id:
                if aweme_id in seen:
                    continue
                seen.add(aweme_id)
            if cutoff_sec and int(item.get("create_time") or 0) <= cutoff_sec:
                removed += 1
                continue
            aweme_list.append(item)

    if not all_pages:
        raise APIError(_("浏览器通道未获取到任何作品页"))

    last_cursor = int(all_pages[-1].get("max_cursor") or 0)
    # 本地补丁(Fix D)：增量截断触发时把返回 cursor 压到窗口下限之下——
    # handler 的窗口检查(max_cursor < min_cursor → break)立即生效，
    # 省一次 45s 翻页等待与多余的空调用（min_cursor=0 全量场景不压）。
    if min_cursor:
        last_cursor = min(last_cursor, int(min_cursor) - 1)

    # 记录聚合终点，供 handler 循环回来的下一次调用干净终止
    _SESSION_END_CURSOR[sec_user_id] = last_cursor

    # 全量核对报告(2026-09-16 共识)：全量语义(min_cursor=0)且主页总数已知时，
    # 核对已收与主页计数；缺口醒目报告（可能含私密/仅自己可见作品），任务按
    # 部分完成处理——是否补拉由用户决定（再跑一次全量，已下载自动跳过）。
    collected_n = len(aweme_list)
    if total and min_cursor == 0:
        if collected_n < total:
            logger.warning(
                _(
                    "⚠ 全量核对：已拉 {0} / 主页作品 {1} 条，缺 {2} 条"
                    "（缺口可能含私密/仅自己可见作品；如需补拉可再跑一次全量，"
                    "已下载自动跳过）"
                ).format(collected_n, total, total - collected_n)
            )
        else:
            logger.info(
                _("全量核对通过：已拉 {0} / 主页作品 {1} 条").format(
                    collected_n, total
                )
            )

    if not aweme_list:
        # 本地补丁(Fix A, 2026-09-16)：会话正常拉到作品、但全部在增量窗口
        # 外被逐条剔除 = "窗口内无新作品"——这是追更最常见日常，不是失败。
        # 返回空列表干净终止（任务正常成功、last_run 正常推进），不再误判
        # 为浏览器通道失败而回退 httpx 撞 Argus（"一一"任务事故修复）。
        logger.info(
            _(
                "窗口内无新作品：{0} 条作品全部早于阈值 {1}，"
                "本次无需下载（任务正常结束）"
            ).format(removed, int(min_cursor or 0))
        )
        return {
            "aweme_list": [],
            "max_cursor": last_cursor,
            "has_more": False,
            "status_code": 0,
        }

    logger.info(
        _(
            "浏览器通道完成:{0} 次会话 / {1} 条作品(剔除窗口外 {2} 条,"
            "停止原因={3},{4})"
        ).format(
            sessions,
            len(aweme_list),
            removed,
            stop_reason or "unknown",
            _("无头 Firefox") if used_headless else _("有头 Firefox"),
        )
    )
    # 单次调用任务式聚合:一次给足,has_more 置 False 终止 handler 翻页
    return {
        "aweme_list": aweme_list,
        "max_cursor": last_cursor,
        "has_more": False,
        "status_code": 0,
    }
