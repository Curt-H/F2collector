# ruff: noqa
"""端到端验证:post 列表全自动浏览器通道(滚动 CD/续滚/逐条精确停止)。

场景:
  1_direct_browser    直接调 fetch_user_post(全量):浏览器主通道 + CD 节奏
  2_filter_compat     返回值经 UserPostFilter 解析(下游零改动)
  3_session_end       同用户带终点 cursor 二次调用 → 空列表干净终止
  4_httpx_fallback    浏览器通道失败 → 回退 httpx 拿到数据
  5_depth_cutoff      min_cursor=未来时间 → 首页后即停(end_reason=min_cursor)
  6_per_item_filter   min_cursor=中间时刻 → 逐条剔除窗口外作品且停止翻页
  7_renew_session     模拟滚动连续失败 → 自动续滚 → 跨会话去重聚合
"""
import asyncio
import sys
import time
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]  # scripts/ 下上跳一级到仓库根
sys.path.insert(0, str(TOOL_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from probe_browser_post import load_cookie  # noqa: E402
from f2.apps.douyin import browser_fetch  # noqa: E402
from f2.apps.douyin.crawler import DouyinCrawler  # noqa: E402
from f2.apps.douyin.model import UserPost  # noqa: E402
from f2.apps.douyin.filter import UserPostFilter  # noqa: E402
from f2.exceptions.api_exceptions import APIError  # noqa: E402

#: 与探针同款对照账号(softFuFu)
SEC = "MS4wLjABAAAAZgFvH5X9vW0JfoAZWxoRPMMwStbsk5hK3AplFyKTfIg"

KWARGS = {
    "cookie": load_cookie(),
    "headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://www.douyin.com/"},
    "proxies": {"http://": None, "https://": None},
    "max_retries": 1,
    "timeout": 15,
    "min_cursor": 0,
}


async def main():
    ok = True
    async with DouyinCrawler(KWARGS) as crawler:
        # ── 场景 1:直接首选浏览器通道(全量,CD 节奏) ──
        params = UserPost(max_cursor=0, count=20, sec_user_id=SEC)
        print("[1_direct_browser] 直接调 fetch_user_post……")
        response = None
        try:
            response = await crawler.fetch_user_post(params)
            n = len(response.get("aweme_list") or [])
            print(
                f"[1_direct_browser] 返回: {n} 条作品, "
                f"max_cursor={response.get('max_cursor')}, has_more={response.get('has_more')}"
            )
            print("[1_direct_browser] ✅" if n > 0 else "[1_direct_browser] ❌")
            ok = ok and n > 0
        except APIError as exc:
            print(
                f"[1_direct_browser] ❌ 双通道皆败(疑似 Argus 收紧窗口): {exc}"
            )
            ok = False

        # ── 场景 2:下游零改动 ──
        if response:
            video = UserPostFilter(response)
            print(
                f"[2_filter_compat] aweme_id={len(video.aweme_id)}条, "
                f"nickname={video.nickname[:1]}, has_aweme={video.has_aweme}"
            )
            good = video.has_aweme and bool(video.nickname)
            print("[2_filter_compat] ✅" if good else "[2_filter_compat] ❌")
            ok = ok and good

    # ── 场景 3:会话终点捷径 ──
    end_cursor = browser_fetch._SESSION_END_CURSOR.get(SEC)
    print(f"[3_session_end] 场景1记录的聚合终点: {end_cursor}")
    if end_cursor is None:
        print("[3_session_end] ⚠ 场景1未成功,跳过(联动场景)")
    else:
        again = await browser_fetch.fetch_user_post_via_browser(
            cookie=KWARGS["cookie"], sec_user_id=SEC, max_cursor=end_cursor
        )
        good = again.get("aweme_list") == [] and again.get("has_more") is False
        print(
            "[3_session_end] ✅ 干净终止(空列表/has_more=False,未再起浏览器)"
            if good
            else "[3_session_end] ❌"
        )
        ok = ok and good

    # ── 场景 4:浏览器失败 → 直接抛 APIError(2026-09-16 修订:无 httpx 兜底) ──
    async with DouyinCrawler(KWARGS) as crawler:
        original = browser_fetch.fetch_user_post_via_browser

        async def _browser_down(*a, **kw):
            raise APIError("模拟:浏览器双模式均失败")

        httpx_calls = {"n": 0}

        async def _stub_fetch(endpoint):
            httpx_calls["n"] += 1
            return {"aweme_list": [], "status_code": 0}

        browser_fetch.fetch_user_post_via_browser = _browser_down
        crawler._fetch_get_json = _stub_fetch
        try:
            try:
                await crawler.fetch_user_post(params)
                print("[4_no_fallback] ❌ 未抛错(不应回退也不应成功)")
                ok = False
            except APIError:
                if httpx_calls["n"] == 0:
                    print(
                        "[4_no_fallback] ✅ 浏览器失败直接抛 APIError, "
                        "零 httpx 请求(收紧窗口内不再烧 8 个 403)"
                    )
                else:
                    print(
                        f"[4_no_fallback] ❌ 仍发了 {httpx_calls['n']} 次 httpx 请求"
                    )
                    ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"[4_no_fallback] ❌ 异常: {exc}")
            ok = False
        finally:
            browser_fetch.fetch_user_post_via_browser = original

    # ── 场景 5:min_cursor=未来时间 → 首页后即停 ──
    future_ms = int(time.time() * 1000) + 365 * 24 * 3600 * 1000
    try:
        result5 = await asyncio.to_thread(
            browser_fetch._collect_pages_sync,
            KWARGS["cookie"], SEC, 0, future_ms, True, 0.0,
        )
    except APIError as exc:
        print(f"[5_depth_cutoff] ⚠ 环境受限跳过: {exc}")
        result5 = None
    print(
        f"[5_depth_cutoff] min_cursor=未来时间 → {len(result5['pages'])} 页, "
        f"end_reason={result5['end_reason']}(预期 min_cursor; 页数 1~2, "
        f"SPA 可能自动预加载一页)"
    )
    good = (
        result5["end_reason"] == "min_cursor"
        and 1 <= len(result5["pages"]) <= 2
    )
    print("[5_depth_cutoff] ✅" if good else "[5_depth_cutoff] ❌")
    ok = ok and good

    # ── 场景 6:逐条精确停止 + 同页剔除 ──
    items = (response or {}).get("aweme_list") or []
    if len(items) >= 4:
        # 取第 3 新作品的发布时刻为阈值:预期仅保留最新 2 条
        cts = sorted(
            (int(a.get("create_time") or 0) for a in items), reverse=True
        )
        cutoff_ms = cts[2] * 1000
        try:
            r6 = await browser_fetch.fetch_user_post_via_browser(
                cookie=KWARGS["cookie"], sec_user_id=SEC, min_cursor=cutoff_ms
            )
        except APIError as exc:
            print(f"[6_per_item_filter] ⚠ 环境受限跳过: {exc}")
        else:
            kept = r6.get("aweme_list") or []
            kept_cts = [int(a.get("create_time") or 0) for a in kept]
            # 动态预期:发布时刻可能并列(同秒批量发布),保留数按阈值现算
            expected_keep = len([ct for ct in cts if ct > cutoff_ms // 1000])
            good = (
                len(kept) == expected_keep
                and all(ct * 1000 > cutoff_ms for ct in kept_cts)
                and r6.get("has_more") is False
            )
            print(
                f"[6_per_item_filter] 阈值={cutoff_ms} → 保留 {len(kept)} 条"
                f"(预期 {expected_keep}), 全部晚于阈值: "
                f"{all(ct * 1000 > cutoff_ms for ct in kept_cts)}"
            )
            print("[6_per_item_filter] ✅" if good else "[6_per_item_filter] ❌")
            ok = ok and good
    else:
        print("[6_per_item_filter] ⚠ 作品不足 4 条,跳过")
    # 清缓存,供后续场景从干净状态开始
    browser_fetch._SESSION_END_CURSOR.pop(SEC, None)

    # ── 场景 7:滚动连续失败 → 自动续滚(受控模拟) ──
    original_collect = browser_fetch._collect_pages_sync
    calls = {"n": 0}

    def mk(aweme_id, ct):
        return {"aweme_id": aweme_id, "create_time": ct}

    def fake_collect(cookie, sec, until_cursor, min_cursor, headless, deadline):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "pages": [
                    {"aweme_list": [mk("a1", 100), mk("a2", 90)],
                     "max_cursor": 90, "has_more": True, "oldest_ct": 90}
                ],
                "end_reason": "scroll_fail",
            }
        return {
            "pages": [
                {"aweme_list": [mk("a1", 100), mk("a2", 90)],
                 "max_cursor": 90, "has_more": True, "oldest_ct": 90},
                {"aweme_list": [mk("a3", 80), mk("a4", 70)],
                 "max_cursor": 70, "has_more": False, "oldest_ct": 70},
            ],
            "end_reason": "has_more",
        }

    browser_fetch._collect_pages_sync = fake_collect
    try:
        r7 = await browser_fetch.fetch_user_post_via_browser(
            cookie=KWARGS["cookie"], sec_user_id=SEC
        )
        ids = sorted(a["aweme_id"] for a in r7.get("aweme_list") or [])
        good = (
            calls["n"] == 2
            and ids == ["a1", "a2", "a3", "a4"]
            and r7.get("max_cursor") == 70
            and r7.get("has_more") is False
        )
        print(
            f"[7_renew_session] 会话数={calls['n']}(预期 2), "
            f"去重聚合={ids}(预期 a1-a4), max_cursor={r7.get('max_cursor')}"
        )
        print("[7_renew_session] ✅" if good else "[7_renew_session] ❌")
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"[7_renew_session] ❌ 异常: {exc}")
        ok = False
    finally:
        browser_fetch._collect_pages_sync = original_collect
        browser_fetch._SESSION_END_CURSOR.pop(SEC, None)

    # ── 场景 8：窗口内无新作品（Fix A 事故复现）——全部剔除后空列表正常返回 ──
    original_collect = browser_fetch._collect_pages_sync
    calls8 = {"n": 0}

    def fake_all_old(cookie, sec, until_cursor, min_cursor, headless, deadline):
        calls8["n"] += 1
        return {
            "pages": [
                {"aweme_list": [mk("old1", 10), mk("old2", 5)],
                 "max_cursor": 5, "has_more": False, "oldest_ct": 5}
            ],
            "end_reason": "min_cursor",
        }

    browser_fetch._collect_pages_sync = fake_all_old
    try:
        browser_fetch._SESSION_END_CURSOR.pop(SEC, None)
        r8 = await browser_fetch.fetch_user_post_via_browser(
            cookie=KWARGS["cookie"], sec_user_id=SEC, min_cursor=50_000
        )
        good = (
            r8.get("aweme_list") == []
            and r8.get("has_more") is False
            and r8.get("max_cursor") <= 49_999  # Fix D：cursor 压到阈值-1 之下（实际值更小则保留）
        )
        print(
            f"[8_no_update] 空列表={r8.get('aweme_list') == []}, "
            f"cursor={r8.get('max_cursor')}(应 ≤ 49999 触发窗口 break), 不抛错"
        )
        print("[8_no_update] ✅" if good else "[8_no_update] ❌")
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"[8_no_update] ❌ 异常: {exc}")
        ok = False
    finally:
        browser_fetch._collect_pages_sync = original_collect
        browser_fetch._SESSION_END_CURSOR.pop(SEC, None)

    # ── 场景 9：total_reached 完美完成 ──
    def fake_collect_total(cookie, sec, until_cursor, min_cursor, headless, deadline):
        return {
            "pages": [
                {"aweme_list": [mk("t1", 100), mk("t2", 90)],
                 "max_cursor": 90, "has_more": False, "oldest_ct": 90}
            ],
            "end_reason": "total_reached",
            "total": 2,
        }

    browser_fetch._collect_pages_sync = fake_collect_total
    browser_fetch._SESSION_END_CURSOR.pop(SEC, None)
    try:
        r9 = await browser_fetch.fetch_user_post_via_browser(
            cookie=KWARGS["cookie"], sec_user_id=SEC
        )
        good = len(r9.get("aweme_list") or []) == 2 and r9.get("has_more") is False
        print(
            "[9_total_reached] ✅ 2/2 条完美完成"
            if good
            else "[9_total_reached] ❌"
        )
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"[9_total_reached] ❌ 异常: {exc}")
        ok = False
    finally:
        browser_fetch._SESSION_END_CURSOR.pop(SEC, None)

    # ── 场景 10：缺口部分完成（total=100 只拉到重叠 21 条，续滚上限动态扩展）──
    orig_cooldown = browser_fetch._RENEW_COOLDOWN_RANGE_SEC
    browser_fetch._RENEW_COOLDOWN_RANGE_SEC = (0, 0)  # 加速测试
    calls10 = {"n": 0}

    def fake_gap(cookie, sec, until_cursor, min_cursor, headless, deadline):
        calls10["n"] += 1
        return {
            "pages": [
                {"aweme_list": [mk(f"g{i}", 100 - i) for i in range(21)],
                 "max_cursor": 80, "has_more": True, "oldest_ct": 80}
            ],
            "end_reason": "scroll_fail",
            "total": 100,
        }

    browser_fetch._collect_pages_sync = fake_gap
    browser_fetch._SESSION_END_CURSOR.pop(SEC, None)
    try:
        r10 = await browser_fetch.fetch_user_post_via_browser(
            cookie=KWARGS["cookie"], sec_user_id=SEC
        )
        n10 = len(r10.get("aweme_list") or [])
        # total=100 → est_pages=5 → max_sessions=max(5, 5//2+1)=5
        good = calls10["n"] == 5 and n10 == 21 and r10.get("has_more") is False
        print(
            f"[10_gap_partial] 会话数={calls10['n']}(预期 5,动态扩展), "
            f"聚合={n10} 条(重叠去重), 部分完成不抛错"
        )
        print("[10_gap_partial] ✅" if good else "[10_gap_partial] ❌")
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"[10_gap_partial] ❌ 异常: {exc}")
        ok = False
    finally:
        browser_fetch._collect_pages_sync = original_collect
        browser_fetch._RENEW_COOLDOWN_RANGE_SEC = orig_cooldown
        browser_fetch._SESSION_END_CURSOR.pop(SEC, None)

    # ── 场景 11：has_more=False 且有缺口 → 正常终点（可能含私密作品）──
    def fake_hasmore_end(cookie, sec, until_cursor, min_cursor, headless, deadline):
        return {
            "pages": [
                {"aweme_list": [mk("p1", 100), mk("p2", 90), mk("p3", 80)],
                 "max_cursor": 80, "has_more": False, "oldest_ct": 80}
            ],
            "end_reason": "has_more",
            "total": 100,
        }

    browser_fetch._collect_pages_sync = fake_hasmore_end
    browser_fetch._SESSION_END_CURSOR.pop(SEC, None)
    try:
        r11 = await browser_fetch.fetch_user_post_via_browser(
            cookie=KWARGS["cookie"], sec_user_id=SEC
        )
        good = (
            len(r11.get("aweme_list") or []) == 3
            and r11.get("has_more") is False
        )
        print(
            "[11_hasmore_gap] ✅ 服务端终点正常返回(3 条), 缺口=私密作品提示"
            if good
            else "[11_hasmore_gap] ❌"
        )
        ok = ok and good
    except Exception as exc:  # noqa: BLE001
        print(f"[11_hasmore_gap] ❌ 异常: {exc}")
        ok = False
    finally:
        browser_fetch._SESSION_END_CURSOR.pop(SEC, None)

    print("=" * 60)
    print("全部通过 ✅" if ok else "存在失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
