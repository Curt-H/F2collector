# f2dl 代码优化审查报告

- **日期**：2026-09-17
- **范围**：`f2dl/` 全部 9 模块 + `f2/` 本地补丁（约 40 处补丁点，上游原样代码不列整改项）+ 根目录验证脚本
- **方法**：静态深读 + README/NOTICE/requirements 交叉印证；风控参数建议仅在证据充分时给出（Q6a 口径）
- **排序标准**：影响 × 触发频率（Q7d）

## 实施摘要

本报告的修复项已全部落地，共 5 个提交（基线后）：

| 提交 | 内容 |
|------|------|
| `716851e` | A1：git 基线（140 文件） |
| `6a283aa` | C1+B1+D3+F2：handler 间隔去重 / post 终点免睡 / 间隔语义 / 死注释清理 |
| `3420f9d` | D1+C5+D4+C6+C4/C3：超时转换 / 原子写配置 / 403 文案 / 裸 except / cookie 简化与守卫 |
| `cd11c2a` | C2+C3+F1：删除冻结的 token_refresh / 微博残留 / 陈旧产物；文档对齐 |
| `03aa52f` | E1：联网 e2e 移出 pytest 收集 / 新增 12 个离线单测 / verify 脚本跨日修复 |
| （后补） | FULL-1：全量更新被 last_run 静默截断成增量——全量改跑剥离 last_run 的临时副本（见文末「缺陷修复」） |

**验证结果**：`pytest f2dl/test/test_f2dl_offline.py` → 12/12 通过；`verify_rerun_modes.py` → 7/7 通过（exit 0）；全部触及文件 py_compile 通过；项目运行时（便携 Python 3.11.9）import 冒烟通过。

---

## P1 — 高影响

### A1. 没有 git 仓库（已修复）
仓库有 `.gitignore` 却从未 `git init`，任何重构无回退兜底。已 init（`main` 分支）并建立基线；`.gitignore` 原有规则恰好覆盖 `dy/`、`*.db`、`logs/`、`f2dl/_cache/`、`python/`，cookie/个人数据不会入库。注：本目录位于不记录所有权文件系统，已按 git 提示将 `D:/DEV/F2DL` 加入全局 `safe.directory`。

### C1. handler.py 同一段补丁复制 12 份（已修复）
"翻页间隔独立可调"块（约 10 行）在 12 个模式循环中复制粘贴。已抽成 `DouyinHandler._page_delay()`，12 处改为一行调用；其中 post 循环为 B1 版本（含终点 break），其余 11 处为行为逐字节等价的纯去重版本。

### E1-①. pytest 地雷与离线测试缺失（已修复）
- `test_browser_fallback.py`（真实联网重型 e2e）→ 更名 `e2e_browser_channel.py`（git mv，100% rename），不再被 `pytest` 误收集；
- 新增 `f2dl/test/test_f2dl_offline.py`：12 个离线单测，覆盖 `validate_interval`、`_resolve_incremental`（含非法降级）、`_batch_get_cookie`（O2 缓存+TTL）、`extract_identifier`、`sanitize_name`（Windows 保留名）、`build_conf`（one 强制 all / 高级默认回填）、`mask`、`paste_cookie` 前置校验——全部不联网、不起浏览器、不触碰真实 cookie 存储。

---

## P2 — 高影响×低频 / 低影响×高频

### B1. 全量模式每任务白睡 40~50 秒（已修复，仅 post 循环）
翻页循环的 `page_interval` sleep 在 `has_more` 判定之后执行；浏览器通道"一次聚合完"，全量场景第一轮聚合完照睡 45s±5s，第二轮才经会话终点捷径（`browser_fetch.py:545-554`）发现结束。修复：post 循环处理完当前页后 `if not video.has_more: break`——省纯等待（17 任务全量批次约省 13 分钟），不减少任何有效请求，零风控暴露变化。增量日常路径本就在 sleep 之前被 Fix D 的窗口截断短路，不受影响。
**范围收窄说明**：其余 11 个 httpx 模式循环的退出结构各异（8 个无 `has_aweme` 空页检查），无证据支撑一律加终点 break，故仅做 C1 去重、保持上游行为——符合 Q6a"证据充分才开口"。

### D1. `--init-config` 超时裸栈崩溃（已修复）
`runner.run_f2(timeout=...)` 抛 `subprocess.TimeoutExpired`，下游 `cli.py` 只捕 `ValueError/FileNotFoundError/RuntimeError`。已在 `confgen.official_defaults` 就地转换为 `RuntimeError`（下游统一按可读错误处理）。

### C5. 任务配置非原子写（已修复）
`confgen.save_conf` 直接 `write_text`，中断可截断配置导致下次重跑报"无法识别任务配置的平台"。改为同目录 `.tmp` 写入 + `os.replace` 原子替换。

### D4. 403 提示文案与行为不符（已修复）
文案仍写"长冷却/1~3 小时"，实际退避已是 60/90/120 秒。已改为如实描述（含 3 轮仍失败时的收紧窗口解释）。

### C6. model.py 裸 `except:`（已修复）
补丁引入的裸 except 会吞 `KeyboardInterrupt/SystemExit`，改为 `except Exception`。

### C2. 冻结的 token_refresh.py 与现行架构矛盾（已删除）
195 行死代码（2026-09-11 冻结未接线），其 `_write_back` 会把 cookie 写进任务配置文件，与 Q4a"配置 cookie 永久为空、CLI 直传"直接矛盾；复活说明也已过时。git 历史保留，可随时找回。相关引用同步清理：core.py 文档、cookie.py 注释、browser_fetch.py ×2、probe_browser_post.py ×2、requirements.txt 注释。注：`f2/apps/douyin/utils.py` 的 `_load_cached_mstoken`（读 mstoken.json）为软失败休眠代码，唯一的写入方已删，保留无害，复活实验时仍可复用。

### C3. 微博残留与文档腐烂（已修复）
- `f2dl/__init__.py` 文档改写：cookie 生命周期对齐 Q4a（每任务强制重抓/用完即清）；
- README.md ×3、README.en.md ×4：移除 `wb\conf\`、`Download\wb\` 等"专注抖音前"的承诺；
- `cookie.grab_cookie` 对非 douyin 平台显式 `NotImplementedError`（域名硬编码 douyin.com，防未来静默抓错站点）；
- 删除 `examples/weibo-template.yaml`。

### E1-②. verify 脚本分类表（联网项待人工执行）

| 脚本 | 属性 | 前置条件 |
|------|------|----------|
| `verify_rerun_modes.py` | ✅ 全 mock 离线 | 无（已跑，7/7 通过） |
| `verify_optimizations.py` O2/O3 | ✅ 离线 | `logs/probe_browser_post_sample.json` 存在 |
| `verify_optimizations.py` O1 | 本地无网 | Firefox 已关闭 |
| `verify_optimizations.py` W1 | 🌐 联网起浏览器 | cookie + Firefox 关闭 |
| `verify_browser_filter.py` | 🌐 联网 | `dy/cookie.yaml` 有 cookie（跑前先抓） |
| `verify_e2e.py` | 🌐 联网 | 第 22 行旧项目根硬编码已改为 `logs/dy_verify` |
| `e2e_browser_channel.py`（原 test_browser_fallback.py） | 🌐 联网重型 e2e | cookie；已移出 pytest 收集 |

---

## P3 — 卫生与小修

| # | 位置 | 问题 | 状态 |
|---|------|------|------|
| C4 | `f2dl/cookie.py` | 10 分支 browser→函数手写字典 | ✅ 改 `getattr` + 兜底校验 |
| F1 | 仓库根/缓存 | `_tmp_rerun.py`（自述 throwaway）、0 字节 `douyin_videos.db`、819KB `e2e_out.log`、`livephoto_probe.json`、`official_weibo.yaml` | ✅ 已删除 |
| F2 | handler.py | 注释掉的 `AsyncVideoDB` 死代码块 ×4 | ✅ 已删除 |
| D3 | handler 间隔语义 | 显式 0 被 `or` 吞掉；小值+±5s 抖动可为负 | ✅ `_page_delay` 内修复（0 合法、抖动 ±min(5, base×20%)、钳非负；默认 45s 行为不变） |
| F1-留 | `douyin_users.db` | 保留（活动数据，非残留；`runner.py` 已固定 CWD=TOOL_ROOT 故路径可预期） | 保留 |
| A2-留 | `python/` 目录 | 仓库内第二份 f2（便携运行时 site-packages，旧副本）；sys.path 顺序保证根目录 vendored 版优先，无功能 bug | 未动，建议保持 gitignored 即可 |
| B2-留 | f2 子进程冷启动 | 每任务起独立 `python -m f2`（1.5~4s × 任务数）；若要继续压挂机时长，下一个台阶是编排层进程内批处理（工程量大，可选） | 未实施 |
| D2-留 | 编排层看门狗 | `run_f2_captured` 无超时，f2 进程僵死（浏览器卡在预算检查之外）时永久等待；30 分钟滚动预算是 f2 进程内部保险丝。可选：编排层加预算+10 分钟的进程级 kill | 未实施 |
| DB-留 | handler/dl 相对 DB 路径 | `AsyncUserDB("douyin_users.db")` 为 CWD 相对路径（上游写法）；CWD 已被 runner.py 固定，行为可预期 | 未动（改动点多、收益低） |

## 风控参数审查结论（Q6a 口径）

**未发现证据充分、值得调整的参数。** 逐项核过：任务间冷却 60~120s、滚动 CD 5~15s（`_SCROLL_CD_RANGE_SEC`）、30 分钟滚动预算、续滚上限、httpx 8 次紧凑重试（`crawler.py` 的 99.6% 概率覆盖计算）、`_COMPACT_BACKOFF_SEC=(0.3,0.5,0.8)` 与 `_ANTI_RISK_BACKOFF_SEC` 双轨——全部有实测注释背书，且 post 链路已绕开 Argus 网关。唯一的省时项 B1 是逻辑修复而非调参（省纯等待，不减请求）。post 模式的 `page_interval=45s` 仅作用于 httpx 模式翻页（post 走浏览器通道后不再触发），维持上游值。

> **变更记录（2026-09-18）**：账号所有者依 Q4c 授权，将批量追更任务间冷却由 60~120s 调整为 30~60s（`_TASK_COOLDOWN_RANGE_SEC`，每批约省 12 分钟）；重试轮冷却保持 60~120s。风险标注：同一登录账号单位时间浏览主页数上升（约 20→30+/小时），暴露面相应增加。依据：浏览器通道后 Argus 暴露面基本清零 + 上一次同方向缩短（2026-09-16，小时级→60~120s）实测存活无回归。

## 缺陷修复（2026-09-17，报告交付后用户实测发现）

### FULL-1. 全量更新被 last_run 静默截断成增量（已修复）

**现象**：重跑已有任务选"全量更新"（或 `rerun-all` 选全量），实际仍只拉上次运行后的新作品。

**根因**：`-i all` 被两代特性反向解释——引擎双轨语义（`handler.py:458-469`）把 "interval=all + conf 含 last_run" 解释为增量窗口下限（为 run_task 重跑同命令路径设计，必须保留），而双模式重跑用 `-i all` 作全量信号。全量运行时真实配置仍携带 last_run → min_cursor=last_run → 浏览器通道按阈值停止滚动并逐条剔除旧作品；主页计数核对（`min_cursor == 0` 门控，`browser_fetch.py:703`）也不运行，截断完全静默。`verify_rerun_modes.py` 的 B/D 场景只断言 CLI 参数（mock 引擎调用），未穿透到该逻辑，故未拦截。

**修复**：全量模式改跑剥离 last_run 的临时配置副本（复用 `confgen.write_temp_conf` 先例）：引擎落入"皆无=0"全量轨 → 真全量扫描 + 主页计数核对自动激活；真实配置 last_run/interval 不受影响，成功后照常推进。增量路径零改动；引擎零改动。

**否决的备选**：改双轨语义（摧毁"重跑同命令即增量"主工作流）；`-i "2000-01-01|今天"` 技巧（显式轨 min_cursor≈0 但核对门控永不触发）；给 f2 CLI 加 full 标志（vendored CLI 面改动更大）。

**回归覆盖**：`verify_rerun_modes.py` 新增场景 H（全量 `-c` 临时副本无 last_run / `-i all` / 真实配置 last_run 推进且 interval 不动 / 临时文件即用即删）；离线单测新增 `test_rerun_full_uses_temp_conf_without_last_run` / `test_rerun_incremental_uses_real_conf_and_window`。

### NOISE-1. 下载重试的 traceback 大框刷屏（已修复）

**现象**：下载高峰期界面出现 `httpx.PoolTimeout` 的整块 rich traceback（"文件区块超时"），观感差。

**来源**：`max_tasks=10` 下载协程共享 `max_connections=5` 连接池，高峰排队超时抛 PoolTimeout——瞬时争用，就地重试（.tmp 断点续传）已自愈；但 `_download_chunks` 的 7 个 except 分支把 `traceback.format_exc()` 与错误详情以 `logger.error` 双发到 RichHandler 控制台（`rich_tracebacks=True` 渲染成大框）与文件两个 handler。

**修复**：`f2/log/logger.py` 的 RichHandler 增加 `file_only` 过滤器；`base_downloader.py` 中 7 处可重试分支的 traceback+详情改为 `extra={"file_only": True}`（只进 `logs/f2-*.log`，界面仅保留单行重试 WARNING 与最终失败结算）；3 处最终失败分支仅 traceback 转 file_only、终态消息保留控制台。

**备注**：根治 PoolTimeout 可把连接池提到与 max_tasks 一致，但会加大 CDN 并发暴露面（风控）且重试已自愈，按 Q6a 纪律维持现状。

## 复核与回滚

```powershell
git log --oneline            # 线性提交，任一步可单独回退
git revert <sha>             # 任一步可单独回退
python -m pytest f2dl/test/test_f2dl_offline.py -q   # 12 passed
python verify_rerun_modes.py                          # 7 场景全过
```

## 已知边界

- B1 的量化收益（全量批次约省 13 分钟）基于 17 个任务 × 45s±5s 的推算，实际以首次全量 rerun-all 观测为准；
- 联网 verify 项（W1/浏览器过滤/e2e 通道）按 Q8(c) 约定留待人工执行，前置条件见上表；
- `_page_delay` 的 D3 语义变更（显式 0 合法、抖动按比例收缩）仅在配置出非默认小值时才与旧行为可感差异，当前 17 个任务配置均为默认 45s，行为不变。

