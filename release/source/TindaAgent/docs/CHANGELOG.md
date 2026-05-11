# CHANGELOG

本文档用于补录 TindaAgent 的版本演进历史，后续按版本持续维护。

> **分类**: `Added` 新增 | `Changed` 变更 | `Fixed` 修复 | `Removed` 移除 | `BREAKING` 破坏性变更 | `Defense` 防御性加固 | `Known Issues` 已知待修

## v1.8.1 - 2026-05-11

### Added

- **文件附件结构化存储** — 文件作为独立 `file` sub-step 存入 assistant content，server 不再正则解析文件前缀，`sendFile` 参数单独传递
- **文件前缀统一检测** — `stripFilePrefix` 同时用于流式渲染和重载恢复，CRLF 行尾兼容

### Fixed

- **文件附件重复存储** — 重载时不再重复写入文件附件
- **终端确认 UI 主题统一** — 确认弹窗从绿色/橙色改为粉色主题
- **终端确认 reasoning 保留** — DeepSeek thinking 模式下 `reasoning_content` 原样保留，确认后工具标记和思考内容正确存储
- **版本检查通知持久化** — 版本更新通知不再一闪而过，存储后前端可重载

### Removed

- **终端内联确认按钮（旧版）** — 废弃的直接 confirm/deny 按钮，已被弹窗交互替代

## v1.8.0 - 2026-05-10

**会话存储全部重写，修复所有已知会话 BUG。**

### Added

- **session_adapter 模块** — 统一的格式转换层：store dict ↔ LLM 消息、store dict ↔ 前端条目、消息构建器、压缩辅助
- **chat_renderer.js** — 聊天渲染逻辑从 chat.html 提取为独立 JS 模块
- **终端独立存储** — 终端输出分离到 `{sid}.terminal.json`，`append_terminal` / `load_terminal` 独立读写
- **Thinking 持久化** — thinking/reasoning 作为 substep 存储，前端折叠显示，刷新不丢失
- **工具结果注入 LLM** — 存储的 `tool_marker` stdout 提取为 `role=tool` 消息，LLM 可看到工具执行结果；stdin（命令文本）也纳入上下文
- **traceback 日志** — 流式和非流式端点异常处理增加 `traceback.print_exc()`

### Changed

- **BREAKING: 会话存储格式重写** — JSONL 列表 → JSON dict（`{"1": {msg}, "2": {msg}}`），每会话一个 `.json`，旧 JSONL 文件首次读取时自动迁移（`_migrate_jsonl_to_dict`）
- **BREAKING: 工具标记字段重命名** — `tool_name` → `name`、`call_id` → `id`，server / session_adapter / session_store 三处统一
- **session_store 全面重构** — `append_messages` / `append_to_last_assistant` 原子追加（session 级锁）、`_normalize_entry` 新旧格式透明归一化、`_render_exports_for_session` dict 导出、`compress_context` dict 操作、`get_context_messages` 委托 adapter

### Fixed

- **CLI `'str' object has no attribute 'get'`** — `_maybe_generate_title` 和 `/last` 中 `for m in dict` 遍历 key 而非 value，改为 `rows.values()`
- **Web `[error/chat] 'str' object has no attribute 'get'`** — `_build_substeps_from_history` 工具 `result` 字段为字符串时 `inner.get()` 崩溃，增加类型守卫
- **system 通知角色** — stream toggle 等通知存为 `role=system` 而非 `role=assistant`，不再污染对话上下文
- **推理内容渲染** — 流式 reasoning 与 content 交错时顺序修正，reset/delta 边界正确 flush，reload 时连续 tool_marker 分组为单块

### Removed

- **旧 `_load_messages`（JSONL）** — 替换为 `_load_messages_raw`（JSON dict）+ 自动迁移
- **旧 `_normalize_message`** — 替换为 `_normalize_entry`，统一新旧格式处理
- **chat.html 内联渲染逻辑** — 提取到 `chat_renderer.js`
- **终端输出混入聊天消息** — 分离到独立 `.terminal.json`，`_normalize_entry` 过滤 `entry_type=terminal`

### Defense

- **`_load_messages_raw` 源头过滤** — 返回的 dict 中非 dict 值自动剔除
- **全链路 `isinstance(entry, dict)` 守卫** — `session_adapter` 4 处入口 + `session_store` 3 处迭代路径

### Known Issues

- 旧 JSONL 迁移中 `tool_marker` 条目附加到前一条 assistant 的逻辑不完整（`_migrate_jsonl_to_dict` 内 `pass`），迁移后旧工具标记可能丢失
- `_render_exports_for_session` 对 corrupted entry 静默跳过，不报告数据损坏

## v1.7.17 - 2026-05-10

> 此版本为 v1.8.0 的中间迭代，所有变更已合并至 v1.8.0，此处仅保留版本号标记。

## v1.7.16 - 2026-05-07

1. **辅助模型可配置化** — 标题生成和上下文压缩模型不再硬编码，支持 Settings 页面下拉选择 + 环境变量（`TINDA_TITLE_MODEL` / `TINDA_COMPRESS_MODEL`），默认仍为 `deepseek-v4-flash`。优先级：Web Settings > 环境变量 > 默认值。
2. **自动压缩上下文** — 修复自动压缩"三段断链"：补上缺失的 `PATCH /sessions/{id}/config` 端点、context-usage 返回 `max_context_tokens`、前端 `refreshContextUsageLength` 检测阈值超限后自动触发压缩（5 分钟冷却）。
3. **修复 token 计数虚高** — `_estimate_context_usage_length` 新增 `entry_type` 过滤（仅计入 `chat` + `notice`），与 `_store_to_agent_messages` 过滤逻辑对齐，状态栏不再计入终端/工具标记等不入 LLM 的消息。
4. **终端拒绝链路完善** — `agent.py` resume 流程区分 `user_denied` 与正常执行，分别注入不同 system 提示；`tool.py` run_terminal 拒绝返回 `ok: False` + `error_code: user_denied`。
5. **CLI 稳健性增强** — 异常时回滚未完成对话轮次、resume 失败清理 `_held_messages`、版本检测兼容 GitHub API dict 格式、标题修复调用 `SessionManager.set_session_title`。
6. **版本管理加固** — HTTP 下载增加状态码检查、安装失败自动清理残留、switch 失败回滚增加审计事件、schema 校验兼容缺少 `jsonschema` 库、兼容检查增加 `min_compat > max_compat` 非法边界防护、快照包增加符号链接跳过。
7. **权限系统清理** — `terminal_policy.is_bypass_enabled` 改用 `perm.USER_ADMIN` 替换硬编码 511；`tool_runtime` 捕获 `PermissionDeniedError`；`tool.run_tool` 自动注入 `_caller_perm` 参数。
8. **健壮性兜底** — `userdata` 种子用户创建包裹 try/except；`records_store` 修复 chatlike_idx 越界；`CLI/display._find_pending_in_result` 处理 None 输入；`audit.py` 清理冗余 setdefault。

## v1.7.15 - 2026-05-04

1. **CLI 正式发布** — 基于 prompt_toolkit 的 `tinda` 命令：流式对话、Tab 补全、↑↓ 历史、会话管理（/sessions /session /new /delete /reset /last /model /version /quit）、模型选择（↑↓ + Enter）、标题异步生成、启动版本检测。
2. **CLI 终端确认链路** — 完整的 `request → pending → confirm → resume → reply` 工作流，支持 allow/deny、链式命令、多轮确认。
3. **run_terminal 全线加固**：
   - 环境变量脱敏（过滤 KEY/TOKEN/SECRET）
   - `reasoning_content` 原样回传（DeepSeek V4 thinking 模式要求）
   - lone surrogate 编码清理
   - 全部失败检测（不再无脑重试到 max_tool_steps）
   - 错误返回统一携带 `cmd` + `note` 字段
   - 退出码非零时新增 `success: False` 字段
   - 移除死参数 `_confirmed`
   - `cwd` 不存在时返回 `cwd_note` 提示
4. **agent.py 系统提示全面英文化** — 所有注入 LLM 的消息、工具描述、错误提示改为英文。
5. **fewshot 幻觉修复** — 移除伪造对话轮次，身份示例移入 system prompt 文本。
6. **Stream 路径补全** — `stream_chat_with_tools` 同步 `_process_tool_loop` 的全部失败跳断逻辑。
7. **resume 工作流修复** — 确认后强制 LLM 继续回复，空回复时自动生成工具结果摘要。
8. **Web 端会话污染修复** — pending 时不再保存系统提示文字到对话历史。
9. **空会话清理** — 网页端启动时自动清理 `message_count=0` 的会话，CLI 延迟创建（不发消息不留文件）。
10. **CLI 设置持久化** — `~/.tinda/agent/cli-settings.json` 缓存模型和上次会话，支持 `/last` 恢复。
11. **quick.sh / quick.bat / delete.sh / delete.bat** — 一键安装/卸载 `tinda` 系统命令。
12. **pyproject.toml** — 新增 `[project.scripts]` 入口点，`pip install` 后直接可用 `tinda` 命令。
13. **移除 call_backend_tool** — 所有工具直接暴露为 OpenAI native tool_calls，带类型化参数 schema，不再绕网关。
14. **turn_id 全链路贯通** — /chat → pending → /terminal/confirm 统一携带 turn_id，前端同一轮回复合并到单个气泡，不再断裂。
15. **tool marker `>_<` 保留** — done 事件不再覆盖 streamText，标记持久化到气泡中。
16. **409 恢复路径** — /terminal/confirm 不再因 agent 淘汰返回 409，直接从 _terminal_pending 执行命令并重建上下文继续对话。
17. **气泡不连续修复** — 确认回复追加到同 turnId，fallback 路径补上 turnId 参数。
18. **多命令链全部放行** — && ; | || 不再拦截，一次 run_terminal 一次确认。

## v1.7.14 - 2026-05-02

1. server.py 大瘦身（3820 → 2472 行），模型列表/日志读取/脱敏/token 估算推回各自模块，Web 层只保留路由与核心业务。
2. 恢复 v1.7.8 Header 设计：快捷按钮完全动态渲染（QUICK_BUTTON_DEFS）、账户切换改为单按钮 + Popup 卡片。
3. 新增 /settings 路由（之前页面存在但未注册）。
4. 新增 doctor 诊断工具（doctor.py / doctor.bat / doctor.sh）。
5. 启动时序修复：延迟 3s 后自检 30 次（~18s 窗口），通过才开浏览器。
6. 新增 12 个测试：Header v1.7.8 校验、端口重试、启动/停止脚本、状态脚本、日志归档回退、settings 路由等。
7. 新增 DEVELOPMENT_POLICY.md、WSL_WINDOWS_ACCESS.md。
8. CHANGELOG 精简整理。
9. 修复终端确认会话态：新增内存级 pending registry（`_terminal_pending`）和 `GET /terminal/pending`，前端可在确认态漂移时重同步。
10. 修复终端确认严格校验：`POST /terminal/confirm` 增加 `no_pending_for_session`、`confirm_id_not_found_or_expired` 错误码，防止误确认。
11. 修复确认链路重复执行：确认接口不再在 Web 层提前执行 `run_terminal`，统一由 Agent 恢复流程执行一次，避免重复命令。
12. 修复 pending 态丢失：`_get_agent(... preserve_pending=True)` 在确认流程中避免回灌清空挂起态，降低 “no pending confirmation for this session” 假报错。
13. 新增终端确认回归测试：覆盖 pending 列表查询、确认错误码、chat pending 拦截、preserve_pending 不重载、前端无随机 confirm_id 兜底。

## v1.7.9 - 2026-04-29

1. 日志查看器视觉统一：采用终端面板同款粉色渐变背景、macOS 三色点头部、JetBrains Mono 等宽字体、自定义粉色滚动条。
2. 设置页快捷按钮改为操作入口 + 显示开关双栏设计，可直接从设置页跳转日志/模型检测。

## v1.7.8 - 2026-04-29

1. 新增设置页面（`/settings`）：延续可爱风格，集中管理流式输出、终端、token 阈值、快捷按钮等配置项。
2. 聊天页 Header 重构：快捷按钮系统（动态渲染 + 用户自选）、账户切换改为单一按钮 + 弹出卡片窗口、仅保留设置与返回主页为固定按钮。
3. Header 布局简化：默认显示模型切换、流式输出、终端、压缩上下文四个快捷按钮，其余可在设置页开启。

## v1.7.5 - 2026-04-27

1. 新增独立模型检测页（`/model-diagnostics`），支持单项检测与一键全部检测。
2. 新增模型能力检测 API（`POST /model-diagnostics/run`），覆盖连接测试、思考支持测试、图片 URL 测试、视频 URL 测试。
3. 聊天页新增“模型检测”入口按钮，并按 LLM 权限位（`PUBLIC_EXECUTE`）控制显示。

## v1.7.4 - 2026-04-27

1. 终端性能优化：新增终端行数上限裁剪（最多 500 行）、会话事件批量写入（缓冲合并）、工具事件批量 DOM 渲染，显著降低长会话卡顿。
2. 终端显示优化：报错输出统一高亮，刷新/重载后保持错误颜色。
3. 终端结构优化：同一次工具请求（cmd 到 sep）合并为单个终端气泡，便于阅读与定位。
4. 修复审计调用兼容：`audit_event` 同时兼容位置参数与关键字参数，避免旧调用链触发参数错误。

## v1.7.3 - 2026-04-27

1. 修复用户管理入口权限一致性：聊天页与首页统一按 `USER_ADMIN(511)` 判断显示“用户管理”，并在 `/user-admin` 后端路由增加权限拦截与安全回跳（优先 `next/referer`）。
2. 修复版本状态长期分裂：新增运行时版本自愈对齐逻辑，`/system/version` 增加 `effective_version` 与 `switch_enabled`，检测到历史 `current.json` 偏移时自动对齐运行版本。
3. 增强聊天页版本气泡：点击版本气泡时即时调用 `/system/versions` 检查远端最新版本，明确提示“已是最新/发现新版本/检查失败”。
4. 优化工具事件轮询错误体验：`/sessions/{id}/tool-events` 网络抖动（如 `Failed to fetch`）改为节流与连续失败再提示，避免工具成功后仍高频刷红错。
5. 增强日志 ID 兼容解析：`get_log_event_by_id` 支持 `log#123` / `log-123` / `log:123` 等输入格式，兼容原有 `#123` 与 `tc_` 前缀。

## v1.7.2 - 2026-04-26

1. 修复聊天页“思考气泡”残留：流式请求失败时，统一清理 typing 占位与临时 bot 气泡，避免错误消息出现后仍残留伪回复气泡。
2. 优化发送流程清理逻辑：`sendMessage()` 增加统一清理函数，覆盖流式/非流式成功与异常分支，减少分支遗漏。
3. 同步版本号到 `1.7.2`（`pyproject` / 首页版本徽章 / 聊天页版本兜底 / Agent 兜底版本）。

## v1.7.1 - 2026-04-26

1. 修复版本显示与切换一致性：明确区分“运行版本”和“已选版本”，解决历史 `current.json` 与运行代码版本混淆问题。
2. 修复多运行时根分叉并完成数据迁移：新增跨根迁移脚本，将 `/mnt/e/.tinda/agent` 合并迁移到 WSL 主目录并生成迁移报告。
3. 强化版本包可运行性校验：切换前校验版本包入口文件，阻断“空壳包/假切换”。
4. 增强旧包兼容：支持旧结构 `app/Web/server.py` 与新结构 `app/TindaAgent/Web/server.py` 双入口切换启动。
5. 固化发版包流程：新增“按当前源码版本自动生成快照包”能力，并强制快照版本号与 `pyproject.toml` 一致。

## v1.6.8 - 2026-04-26

1. 修复版本接口语义：`/system/version` 返回当前切换指针版本（`current.json`），不再错误显示静态包版本。
2. 修复版本列表降级：`/system/versions` 在远端 GitHub 超时时仍返回本地版本列表，前端保持可浏览本地版本。
3. 优化版本管理弹窗渲染：版本号强制单行显示，说明信息下移到版本号下方，并统一版本项高度层级。

## v1.6.7 - 2026-04-26

1. 新增版本管理引擎：支持 GitHub Releases 检测、版本安装、版本切换、兼容检查。
2. 新增版本签名链路：采用 `manifest.json + manifest.sig`（Ed25519）验签，生成 `signature_id`。
3. 新增运行时多版本目录机制：`~/.tinda/agent/versions/<version>` 与 `current.json` 指针切换。
4. 新增数据兼容框架：共享数据目录 + 自动迁移 + 失败回滚骨架。
5. 补齐发布目录缺失核心模块（paths/migration/observability/security/permission/session/tool runtime），恢复 `release/source` 主线可运行性。

## v1.6.6 - 2026-04-26

1. 修复对话上下文污染：新增历史构建过滤规则，`terminal/tool_marker` 及手动工具命令不再回注到 LLM 上下文。
2. 强化工具链路审计：`agent_ready` 增加上下文过滤统计，便于定位“首轮正常、次轮异常写入”类问题。
3. 优化 Web 兼容逻辑：继续保留前端版本接口动态读取，并同步更新所有版本兜底位点至 `1.6.6`。

## v1.6.5 - 2026-04-26

1. 修复版本来源不一致问题：统一版本读取优先级为 `pyproject.toml`，避免残留 `egg-info (0.1.0)` 覆盖真实版本。
2. 新增后端版本接口 `GET /system/version`，前端版本徽章与版本提示改为运行时读取，不再依赖硬编码显示。
3. 修复日志文件命名落地问题：审计内部错误主写入 `~/.tinda/agent/log/error.log`，同时兼容镜像 `audit_error.log`。
4. 对齐原项目目录与发布目录的版本位点差异，统一更新到 `1.6.5`（README / Web / Agent / pyproject / CHANGELOG）。

## v1.6.4 - 2026-04-26

1. 重构 LLM 层为 Provider Adapter 架构，新增 `ProviderAdapter` 抽象接口。
2. 新增 `OpenAICompatibleProviderAdapter`，保留当前 DeepSeek 接入方式并降低耦合。
3. 更新架构文档并完成版本位点同步（README / Web / Agent / pyproject）。

## v1.6.3 - 2026-04-26

1. 新增后端统一错误日志模块，异常写入 `Data/Log/error.log`（含 traceback）。
2. 接入 Web 与 Tool 关键异常出口，提升问题定位与追踪能力。
3. 版本标识统一升级到 `1.6.3`（README / Web / Agent / pyproject 同步）。

## v1.6.2 - 2026-04-25

1. 更新了持久化数据存储路径。
2. 新增“通过 ID 搜索原日志”的工具与链路。
3. 修复部分模型回复“声称已调用工具但未实际执行”的流程问题（调用一致性增强）。

## v1.5.3 - 2026-04-25

1. 完成大版本整理，统一 Web/Agent/版本提示等核心版本标识。
2. 持续完善会话与消息显示链路，修复多轮加载下的气泡错位与内容追加异常。
3. 优化工具调用与终端展示协作流程，为后续权限/日志接入打基础。

## v0.1.5 - 2026-04-25

1. 初步形成“会话管理 + 工具调用 + 终端输出”一体化交互形态。
2. 完成多项前端可用性修复（版本提示、工具提示、渲染一致性）。
3. 推进聊天记录持久化与加载机制，逐步稳定刷新后的还原表现。

## v0.1.0 - Early Stage

1. 建立项目基础结构（Web / Process / Tool / User / Permission / Output / Master 模块）。
2. 打通最小可用链路：FastAPI Web 路由 → Agent 系统提示 + 上下文管理 → LLMClient DeepSeek API 调用 → 工具注册/分发 → 页面输出。
3. 基础工具集：echo、get_current_time、summarize_text、extract_keywords、run_terminal（早期版本）。
4. 用户系统骨架：多用户登录、权限位掩码、token 认证。
5. 会话存储初版：JSONL 行格式，基础 append/load。
6. 前端：纯 HTML/CSS/JS，粉色主题，聊天界面 + 首页。
7. 版本提示 badge（静态硬编码）。

## 历程备注

1. 项目由 Tinda 持续迭代推进，重点方向为“可用性、可维护性、可扩展性”。
2. 早期部分变更未完整记录到正式变更日志，本文件为补录起点。
