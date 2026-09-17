# ruff: noqa
"""P0 抖音风控探针（只读实验,不改动 F2 任何代码）。

背景：2026-09-15 用户 POST 列表接口 /aweme/v1/web/aweme/post/ 在 14 点与
16 点两个时段均 8/8 全部 403,而单作品链路(移动 Feed 主路径)正常。
本探针用最小请求量量化各缓解手段的当前通过率:

  A mobile_feed      移动端 Feed 单作品接口(免签对照组,预期 200)
  B post_full        Web post 接口 + 完整参数包(复现今天的问题基线)
  C post_min         Web post 接口 + 精简参数(reference 实测指纹参数无益)
  D post_full_ttwid  B + 动态注册 ttwid(reference 的免 Cookie 思路)
  E post_min_ttwid   C + 动态注册 ttwid
  F ssr_user         iesdouyin 移动分享页用户 SSR(免签,验证第一页列表)
  G detail_min_ttwid Web detail 接口 + 精简参数 + ttwid(单作品对照组)

每通道轮次内每次都重新生成 msToken + a_bogus(轮换签名假设一并验证)。
"""

import asyncio
import random
import re
import string
import sys
import warnings
from pathlib import Path

import httpx
import yaml

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

from f2.apps.douyin.api import DouyinAPIEndpoints as ep  # noqa: E402
from f2.apps.douyin.utils import ABogusManager  # noqa: E402

warnings.filterwarnings("ignore", category=Warning)
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SEC_USER_ID = "MS4wLjABAAAAZgFvH5X9vW0JfoAZWxoRPMMwStbsk5hK3AplFyKTfIg"  # softFuFu
AWEME_ID = "7545092436977323316"  # 历史下载样本(对照组)
ROUNDS = 4  # 每通道轮次(总请求 ~28 个,量可控)
INTERVAL_SEC = 1.6  # 轮次间隔,避免探针自身触发频率风控

PC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
)
APP_UA = (
    "com.ss.android.ugc.aweme/290101 (Linux; U; Android 10; zh_CN; Pixel 4; "
    "Build/QQ3A.200805.001; Cronet/TTNetVersion:5f9037be 2023-01-13 QuicVersion:4668bb42 2022-11-21)"
)


def gen_false_mstoken(length: int = 116) -> str:
    base = string.ascii_letters + string.digits + "="
    return "".join(random.choice(base) for _ in range(length))


def load_cookie() -> str:
    path = TOOL_ROOT / "dy" / "cookie.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return str((data.get("douyin") or {}).get("cookie") or "")
    except Exception:
        return ""


async def register_ttwid(client: httpx.AsyncClient) -> str:
    """reference-media-parser 的动态 ttwid 注册(免 Cookie)。"""
    payload = {
        "region": "cn",
        "aid": 6383,
        "need_t": 1,
        "service": "www.douyin.com",
        "migrate_priority": 0,
        "cb_url_protocol": "https",
        "domain": ".douyin.com",
    }
    try:
        resp = await client.post(
            "https://ttwid.bytedance.com/ttwid/union/register/", json=payload
        )
        ttwid = resp.cookies.get("ttwid") or ""
        if ttwid:
            print(f"[ttwid] 动态注册成功: {ttwid[:24]}...")
        else:
            print("[ttwid] 动态注册失败(响应无 ttwid cookie)")
        return ttwid
    except Exception as exc:  # noqa: BLE001
        print(f"[ttwid] 动态注册异常: {exc}")
        return ""


def build_pc_headers(cookie: str) -> dict:
    h = {
        "User-Agent": PC_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": f"https://www.douyin.com/user/{SEC_USER_ID}",
        "sec-ch-ua": '"Microsoft Edge";v="130", "Chromium";v="130", "Not?A_Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if cookie:
        h["Cookie"] = cookie
    return h


def full_params(ms_token: str) -> dict:
    """F2 UserPost(BaseRequestModel) 的完整默认参数包(与今天 403 日志一致)。"""
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": 1,
        "publish_video_strategy_type": 2,
        "pc_libra_divert": "Windows",
        "version_code": "290100",
        "version_name": "29.1.0",
        "cookie_enabled": "true",
        "screen_width": 1920,
        "screen_height": 1080,
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Edge",
        "browser_version": "130.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "130.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": 12,
        "device_memory": 8,
        "platform": "PC",
        "downlink": 10,
        "effective_type": "4g",
        "round_trip_time": 100,
        "msToken": ms_token,
        "max_cursor": 0,
        "count": 20,
        "sec_user_id": SEC_USER_ID,
    }


def min_params(ms_token: str) -> dict:
    """精简参数包:只留接口必需项(按 reference 的参数精简经验)。"""
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "version_code": "290100",
        "max_cursor": 0,
        "count": 20,
        "sec_user_id": SEC_USER_ID,
        "msToken": ms_token,
    }


def detail_min_params(ms_token: str) -> dict:
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "version_code": "290100",
        "aweme_id": AWEME_ID,
        "msToken": ms_token,
    }

async def probe_api(client, name, base_endpoint, params_fn, headers, results):
    """每轮重签(轮换 msToken + a_bogus),统计通过率。"""
    ok, fail, samples = 0, 0, []
    for i in range(ROUNDS):
        url = ABogusManager.model_2_endpoint(
            PC_UA, base_endpoint, params_fn(gen_false_mstoken())
        )
        try:
            resp = await client.get(url, headers=headers)
            status = resp.status_code
            if status == 200:
                ok += 1
                try:
                    data = resp.json()
                except Exception:
                    data = {}
                has_aweme = bool(data.get("aweme_list")) or bool(
                    data.get("aweme_detail")
                )
                samples.append(
                    f"200 has_aweme={has_aweme} status_code={data.get('status_code')}"
                )
            else:
                fail += 1
                samples.append(f"{status} {resp.text[:90]}")
        except Exception as exc:  # noqa: BLE001
            fail += 1
            samples.append(f"EXC {type(exc).__name__}: {str(exc)[:90]}")
        if i < ROUNDS - 1:
            await asyncio.sleep(INTERVAL_SEC)
    results[name] = {"ok": ok, "fail": fail, "samples": samples}
    print(f"[{name}] 通过 {ok}/{ROUNDS}")
    for s in samples[:2]:
        print(f"    - {s}")
    await asyncio.sleep(INTERVAL_SEC)


async def probe_mobile_feed(client, results):
    """移动端 Feed 单作品接口:免签对照组(主节点一次即可)。"""
    ok, fail, samples = 0, 0, []
    url = f"{ep.MOBILE_FEED}?aweme_id={AWEME_ID}&aid=1128&version_code=290100"
    try:
        resp = await client.get(
            url, headers={"User-Agent": APP_UA}, follow_redirects=True
        )
        if resp.status_code == 200:
            data = resp.json()
            hit = any(
                str(item.get("aweme_id")) == AWEME_ID
                for item in (data.get("aweme_list") or [])
            )
            ok += 1
            samples.append(
                f"200 aweme_list={len(data.get('aweme_list') or [])} hit={hit} "
                f"status_code={data.get('status_code')}"
            )
        else:
            fail += 1
            samples.append(f"{resp.status_code} {resp.text[:90]}")
    except Exception as exc:  # noqa: BLE001
        fail += 1
        samples.append(f"EXC {type(exc).__name__}: {str(exc)[:90]}")
    results["A_mobile_feed"] = {"ok": ok, "fail": fail, "samples": samples}
    print(f"[A_mobile_feed] 通过 {ok}/1")
    for s in samples:
        print(f"    - {s}")
    await asyncio.sleep(INTERVAL_SEC)


async def probe_ssr_user(client, results, cookie: str):
    """iesdouyin 移动分享页用户 SSR:免签,验证是否内嵌第一页作品列表。"""
    url = f"https://www.iesdouyin.com/share/user/{SEC_USER_ID}"
    headers = {
        "User-Agent": MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Referer": "https://www.douyin.com/?is_from_mobile_home=1&recommend=1",
    }
    if cookie:
        headers["Cookie"] = cookie
    ok, fail, samples = 0, 0, []
    try:
        resp = await client.get(url, headers=headers, follow_redirects=True)
        html = resp.text or ""
        marker_router = "_ROUTER_DATA" in html
        marker_render = "RENDER_DATA" in html
        ids = set(re.findall(r"\b(7\d{18})\b", html))
        if resp.status_code == 200 and (marker_router or marker_render):
            ok += 1
            samples.append(
                f"200 len={len(html)} _ROUTER_DATA={marker_router} "
                f"RENDER_DATA={marker_render} 19位ID数={len(ids)}"
            )
            if ids:
                samples.append(f"    样本ID: {sorted(ids)[:3]}")
        else:
            fail += 1
            samples.append(f"{resp.status_code} len={len(html)} {html[:80]!r}")
    except Exception as exc:  # noqa: BLE001
        fail += 1
        samples.append(f"EXC {type(exc).__name__}: {str(exc)[:90]}")
    results["F_ssr_user"] = {"ok": ok, "fail": fail, "samples": samples}
    print(f"[F_ssr_user] 通过 {ok}/1")
    for s in samples:
        print(f"    {s}")


async def main():
    cookie = load_cookie()
    print("=" * 70)
    print(f"P0 抖音风控探针 | 用户={SEC_USER_ID[:20]}... | ROUNDS={ROUNDS}")
    print(
        f"cookie.yaml: "
        f"{'有用户 cookie(长度 %d)' % len(cookie) if cookie else '空(匿名探针)'}"
    )
    print("=" * 70)

    async with httpx.AsyncClient(verify=False, timeout=15.0, http2=False) as client:
        ttwid = await register_ttwid(client)
        cookie_ttwid = f"ttwid={ttwid}" if ttwid else ""
        if cookie and cookie_ttwid:
            cookie_ttwid = f"{cookie}; {cookie_ttwid}"

        results = {}
        await probe_mobile_feed(client, results)
        h = build_pc_headers(cookie)
        await probe_api(client, "B_post_full", ep.USER_POST, full_params, h, results)
        await probe_api(client, "C_post_min", ep.USER_POST, min_params, h, results)
        h_ttwid = build_pc_headers(cookie_ttwid)
        await probe_api(
            client, "D_post_full_ttwid", ep.USER_POST, full_params, h_ttwid, results
        )
        await probe_api(
            client, "E_post_min_ttwid", ep.USER_POST, min_params, h_ttwid, results
        )
        await probe_ssr_user(client, results, cookie_ttwid)
        await probe_api(
            client,
            "G_detail_min_ttwid",
            ep.POST_DETAIL,
            detail_min_params,
            h_ttwid,
            results,
        )

        print("=" * 70)
        print("汇总(通过/总数):")
        for name, r in results.items():
            total = r["ok"] + r["fail"]
            print(f"  {name:<22}{r['ok']}/{total}")
        print("=" * 70)
        print("判定: post/detail 接口 HTTP 200 且含数据即视为通过;")
        print("      403/4xx 均视为被 Argus 网关拦截。")


if __name__ == "__main__":
    asyncio.run(main())
