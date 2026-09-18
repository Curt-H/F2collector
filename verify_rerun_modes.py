# ruff: noqa
"""验证:双模式重跑(增量/全量询问)的参数构建与默认值逻辑(mock,不真下载)。"""
import sys
import types
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import yaml  # noqa: E402
from datetime import datetime  # noqa: E402

from f2dl import core  # noqa: E402

ok = True

CONF_DIR = TOOL_ROOT / "logs" / "_rerun_test_conf"
CONF_DIR.mkdir(parents=True, exist_ok=True)


def make_conf(name: str, *, last_run: str = "", interval: str = "all") -> Path:
    conf = CONF_DIR / f"{name}.yaml"
    section = {
        "url": "https://www.douyin.com/user/MS4wLjABAAAAcyxzJ0N09Pu2eThdrBvzKuboNLC_j75ThyKJvoNds1Y",
        "mode": "post",
        "interval": interval,
    }
    if last_run:
        section["last_run"] = last_run
    conf.write_text(
        yaml.dump({"douyin": section}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return conf


captured = {}
core._obtain_cookie = lambda pk, browser, interactive=True: "fake_cookie"
core._batch_get_cookie = lambda pk, browser: "fake_cookie"


def _fake_retry(args, platform_key="", task_label=""):
    captured["args"] = list(args)
    # 快照本次实际传给 f2 的配置内容（FULL-1 回归测试用）
    _c_path = Path(args[args.index("-c") + 1]) if "-c" in args else None
    captured["conf_snapshot"] = (
        yaml.safe_load(_c_path.read_text(encoding="utf-8")) if _c_path else None
    )
    return 0


core._run_with_retry = _fake_retry

PROMPT_RET = {"v": "1"}
core.click.prompt = lambda *a, **k: PROMPT_RET["v"]


class FakeStdin:
    tty = True

    def isatty(self):
        return self.tty


core.sys = types.SimpleNamespace(stdin=FakeStdin())


def set_tty(v: bool):
    FakeStdin.tty = v


def get_i_value(args):
    return args[args.index("-i") + 1] if "-i" in args else "(none)"


LR = "2026-09-15 13:54:54"
# 预期窗口动态取"今天"（原脚本硬编码编写当日，跨日必假失败）
EXP_WINDOW = f"2026-09-15|{datetime.now():%Y-%m-%d}"

# ── A：交互选 1（增量）→ 注入 last_run 窗口 ──
PROMPT_RET["v"] = "1"
conf = make_conf("a_incr", last_run=LR)
rc = core.rerun_task(conf, browser="firefox")
iv = get_i_value(captured["args"])
report_a = rc == 0 and iv == EXP_WINDOW
print(f"[A_incremental] rc={rc}, -i={iv} (预期 {EXP_WINDOW})")
print("[A_incremental] ✅" if report_a else "[A_incremental] ❌")
ok = ok and report_a

# ── B：交互选 2（全量）→ 注入 -i all ──
PROMPT_RET["v"] = "2"
conf = make_conf("b_full", last_run=LR)
rc = core.rerun_task(conf, browser="firefox")
iv = get_i_value(captured["args"])
report_b = rc == 0 and iv == "all"
print(f"[B_full] rc={rc}, -i={iv} (预期 all)")
print("[B_full] ✅" if report_b else "[B_full] ❌")
ok = ok and report_b

# ── C：非交互（stdin 非 tty）未指定 → 默认增量 ──
PROMPT_RET["v"] = "2"  # 若误询问会选 2（all），应实际走增量窗口
set_tty(False)
conf = make_conf("c_nonint", last_run=LR)
rc = core.rerun_task(conf, browser="firefox", interactive=False)
iv = get_i_value(captured["args"])
report_c = rc == 0 and iv == EXP_WINDOW
print(f"[C_noninteractive_default] rc={rc}, -i={iv} (预期增量窗口 {EXP_WINDOW})")
print("[C_noninteractive_default] ✅" if report_c else "[C_noninteractive_default] ❌")
ok = ok and report_c
set_tty(True)

# ── D：显式区间任务 + 全量 → -i all（临时覆盖）──
PROMPT_RET["v"] = "2"
conf = make_conf("d_explicit_full", last_run=LR, interval="2024-01-01|2024-12-31")
rc = core.rerun_task(conf, browser="firefox", update_mode="full")
iv = get_i_value(captured["args"])
conf_data = yaml.safe_load(conf.read_text(encoding="utf-8"))
interval_untouched = conf_data["douyin"]["interval"] == "2024-01-01|2024-12-31"
report_d = rc == 0 and iv == "all" and interval_untouched
print(
    f"[D_explicit_full] rc={rc}, -i={iv} (预期 all), "
    f"配置原区间未被改动: {interval_untouched}"
)
print("[D_explicit_full] ✅" if report_d else "[D_explicit_full] ❌")
ok = ok and report_d

# ── E：显式区间任务 + 增量 → 不注入 -i（尊重原区间）──
PROMPT_RET["v"] = "1"
conf = make_conf("e_explicit_incr", last_run=LR, interval="2024-01-01|2024-12-31")
rc = core.rerun_task(conf, browser="firefox", update_mode="incremental")
iv = get_i_value(captured["args"])
report_e = rc == 0 and iv == "(none)"
print(f"[E_explicit_incr] rc={rc}, -i={iv} (预期 (none)=尊重原区间)")
print("[E_explicit_incr] ✅" if report_e else "[E_explicit_incr] ❌")
ok = ok and report_e

# ── F：无 last_run + 交互 → 不询问直接全量 ──
PROMPT_RET["v"] = "1"  # 若误询问会选 1（增量），应实际走全量
conf = make_conf("f_no_lastrun")
rc = core.rerun_task(conf, browser="firefox")
iv = get_i_value(captured["args"])
report_f = rc == 0 and iv == "all"
print(f"[F_no_lastrun_full] rc={rc}, -i={iv} (预期 all=不询问直接全量)")
print("[F_no_lastrun_full] ✅" if report_f else "[F_no_lastrun_full] ❌")
ok = ok and report_f

# ── G：未知 update_mode 报错 ──
try:
    core.rerun_task(make_conf("g_bad", last_run=LR), update_mode="bogus")
    print("[G_bad_mode] ❌ 未报错")
    ok = False
except ValueError as exc:
    print(f"[G_bad_mode] ✅ 拒绝非法值: {exc}")
except Exception as exc:  # noqa: BLE001
    print(f"[G_bad_mode] ❌ 异常类型错误: {type(exc).__name__}")
    ok = False

# ── H：全量模式跑剥离 last_run 的临时配置副本（FULL-1 回归）──
# 引擎双轨语义会把 "interval=all + last_run" 解释成增量下限，导致全量被
# last_run 静默截断。修复后：全量的 -c 指向剥离 last_run 的临时副本，
# 真实配置的 last_run/interval 不动，临时文件用后即删。
PROMPT_RET["v"] = "2"
conf = make_conf("h_full_temp", last_run=LR, interval="2024-01-01|2024-12-31")
rc = core.rerun_task(conf, browser="firefox", update_mode="full")
args = captured["args"]
snap = (captured.get("conf_snapshot") or {}).get("douyin") or {}
run_conf_path = Path(args[args.index("-c") + 1]) if "-c" in args else None
real = yaml.safe_load(conf.read_text(encoding="utf-8"))["douyin"]
h_temp_stripped = "last_run" not in snap  # 临时副本无 last_run
h_temp_interval = snap.get("interval") == "2024-01-01|2024-12-31"  # 副本保留原区间
h_temp_cleaned = run_conf_path is not None and not run_conf_path.exists()  # 用后即删
h_all = "-i" in args and args[args.index("-i") + 1] == "all"
h_real_interval = real.get("interval") == "2024-01-01|2024-12-31"  # 原值不动
h_real_advanced = bool(real.get("last_run")) and real.get("last_run") != LR  # 照常推进
report_h = (
    rc == 0 and h_all and h_temp_stripped and h_temp_interval
    and h_temp_cleaned and h_real_interval and h_real_advanced
)
print(f"[H_full_temp_conf] rc={rc}, -i={get_i_value(args)}, -c={run_conf_path}")
print(
    f"[H_full_temp_conf] 临时副本无last_run: {h_temp_stripped}, "
    f"副本保留原区间: {h_temp_interval}, 用后即删: {h_temp_cleaned}, "
    f"真实配置区间不动: {h_real_interval}, last_run 已推进: {h_real_advanced}"
)
print("[H_full_temp_conf] ✅" if report_h else "[H_full_temp_conf] ❌")
ok = ok and report_h

print("=" * 60)
print("全部通过 ✅" if ok else "存在失败 ❌")
sys.exit(0 if ok else 1)
