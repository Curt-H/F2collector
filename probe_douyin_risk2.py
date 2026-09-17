# ruff: noqa
"""P0 抖音风控探针 v2:uifid 假设验证 + SSR 存活检查(精简版)。

v1 结论:匿名 post/detail 全 403 "Uifid Not Found";动态 ttwid、参数精简无效;
移动 Feed 正常。v2 验证:
  J feed_new_id      移动 Feed + 最新作品(命中对照)
  H post_uifid_rand  随机 uifid header(Argus 是存在性检查还是内容校验?)
  I post_uifid_ttwid 随机 uifid + 动态 ttwid
  K share_video_ssr  iesdouyin/share/video/{id} 移动 SSR(单作品次主路径)
  L share_user_dump  share/user HTML 落盘分析
"""
import asyncio, random, re, string, sys, warnings  # noqa
from pathlib import Path
import httpx

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))
from f2.apps.douyin.api import DouyinAPIEndpoints as ep  # noqa: E402
from f2.apps.douyin.utils import ABogusManager  # noqa: E402

warnings.filterwarnings("ignore", category=Warning)
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SEC = "MS4wLjABAAAAZgFvH5X9vW0JfoAZWxoRPMMwStbsk5hK3AplFyKTfIg"
AWEME_ID = "7674503540953550922"  # softFuFu 最新作品
ROUNDS, GAP = 3, 1.2

PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 8.0.0; SM-G955U Build/R16NW) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36")
APP_UA = ("com.ss.android.ugc.aweme/290101 (Linux; U; Android 10; zh_CN; Pixel 4; "
          "Build/QQ3A.200805.001; Cronet/TTNetVersion:5f9037be 2023-01-13)")

results = {}


def rand_hex(n=32):
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def false_ms():
    base = string.ascii_letters + string.digits + "="
    return "".join(random.choice(base) for _ in range(116))


def min_params(ms):
    return {"device_platform": "webapp", "aid": "6383", "channel": "channel_pc_web",
            "version_code": "290100", "max_cursor": 0, "count": 20,
            "sec_user_id": SEC, "msToken": ms}


def hdr(cookie="", uifid=""):
    h = {"User-Agent": PC_UA, "Accept": "application/json, text/plain, */*",
         "Referer": f"https://www.douyin.com/user/{SEC}",
         "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}
    if cookie:
        h["Cookie"] = cookie
    if uifid:
        h["uifid"] = uifid
    return h


async def ttwid_reg(c):
    try:
        r = await c.post("https://ttwid.bytedance.com/ttwid/union/register/", json={
            "region": "cn", "aid": 6383, "need_t": 1, "service": "www.douyin.com",
            "migrate_priority": 0, "cb_url_protocol": "https", "domain": ".douyin.com"})
        return r.cookies.get("ttwid") or ""
    except Exception:
        return ""


async def probe_api(c, name, base, headers):
    ok, samples = 0, []
    for i in range(ROUNDS):
        url = ABogusManager.model_2_endpoint(PC_UA, base, min_params(false_ms()))
        try:
            r = await c.get(url, headers=headers)
            if r.status_code == 200:
                try:
                    d = r.json()
                except Exception:
                    d = {}
                ok += 1
                has = bool(d.get("aweme_list") or d.get("aweme_detail"))
                samples.append(f"200 has_data={has} sc={d.get('status_code')}")
            else:
                samples.append(f"{r.status_code} {r.text[:70]}")
        except Exception as e:
            samples.append(f"EXC {type(e).__name__}")
        if i < ROUNDS - 1:
            await asyncio.sleep(GAP)
    results[name] = ok
    print(f"[{name}] {ok}/{ROUNDS} | {samples[0] if samples else '-'}")
    await asyncio.sleep(GAP)

async def main():
    print("=" * 66)
    print("P0 探针 v2 | uifid 假设验证 + SSR 存活检查")
    print("=" * 66)
    async with httpx.AsyncClient(verify=False, timeout=15.0, http2=False) as c:
        ttwid = await ttwid_reg(c)
        print(f"[ttwid] {'OK' if ttwid else 'FAIL'}")
        uifid = rand_hex(32)

        # J: 移动 Feed + 最新作品
        try:
            r = await c.get(
                f"{ep.MOBILE_FEED}?aweme_id={AWEME_ID}&aid=1128&version_code=290100",
                headers={"User-Agent": APP_UA}, follow_redirects=True)
            if r.status_code == 200:
                d = r.json()
                hit = any(str(i.get("aweme_id")) == AWEME_ID
                          for i in (d.get("aweme_list") or []))
                print(f"[J_feed_new_id] 200 aweme_list={len(d.get('aweme_list') or [])} hit={hit}")
                results["J_feed_new_id"] = 1 if hit else 0
            else:
                print(f"[J_feed_new_id] {r.status_code}")
                results["J_feed_new_id"] = 0
        except Exception as e:
            print(f"[J_feed_new_id] EXC {type(e).__name__}")
            results["J_feed_new_id"] = 0
        await asyncio.sleep(GAP)

        # H: 随机 uifid
        await probe_api(c, "H_post_uifid_rand", ep.USER_POST, hdr(uifid=uifid))
        # I: 随机 uifid + 动态 ttwid
        await probe_api(c, "I_post_uifid_ttwid", ep.USER_POST,
                        hdr(cookie=f"ttwid={ttwid}" if ttwid else "", uifid=uifid))

        # K: share/video 单作品 SSR
        try:
            r = await c.get(
                f"https://www.iesdouyin.com/share/video/{AWEME_ID}",
                headers={"User-Agent": MOBILE_UA,
                         "Referer": "https://www.douyin.com/?is_from_mobile_home=1&recommend=1"},
                follow_redirects=True)
            html = r.text or ""
            ok = r.status_code == 200 and ("_ROUTER_DATA" in html or "RENDER_DATA" in html)
            print(f"[K_share_video_ssr] {r.status_code} len={len(html)} "
                  f"router={'_ROUTER_DATA' in html} render={'RENDER_DATA' in html} "
                  f"videoInfoRes={'videoInfoRes' in html}")
            results["K_share_video_ssr"] = 1 if ok else 0
        except Exception as e:
            print(f"[K_share_video_ssr] EXC {type(e).__name__}")
            results["K_share_video_ssr"] = 0
        await asyncio.sleep(GAP)

        # L: share/user HTML 落盘
        try:
            r = await c.get(
                f"https://www.iesdouyin.com/share/user/{SEC}",
                headers={"User-Agent": MOBILE_UA}, follow_redirects=True)
            html = r.text or ""
            (TOOL_ROOT / "logs" / "share_user_dump.html").write_text(
                html, encoding="utf-8", errors="replace")
            ids = set(re.findall(r"\b(7\d{18})\b", html))
            print(f"[L_share_user_dump] {r.status_code} len={len(html)} "
                  f"router={'_ROUTER_DATA' in html} render={'RENDER_DATA' in html} "
                  f"pace_f={'self.__pace_f' in html} ids19={len(ids)}")
            results["L_share_user_dump"] = 1 if ("_ROUTER_DATA" in html or ids) else 0
        except Exception as e:
            print(f"[L_share_user_dump] EXC {type(e).__name__}")
            results["L_share_user_dump"] = 0

        print("=" * 66)
        for k, v in results.items():
            print(f"  {k:<24}{v}")


if __name__ == "__main__":
    asyncio.run(main())
