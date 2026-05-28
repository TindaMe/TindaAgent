# CHANGELOG

本文档用于补录 TindaAgent 的版本演进历史，后续按版本持续维护。

> **分类**: `Added` 新增 | `Changed` 变更 | `Fixed` 修复 | `Removed` 移除 | `BREAKING` 破坏性变更 | `Defense` 防御性加固 | `Known Issues` 已知待修

## Unreleased

## v1.11.0 - 2026-05-28

### BREAKING

- **Python 栈彻底移除** — 删除所有 Python 源码、Python 测试、`__pycache__`、`pyproject.toml`、`requirements.txt`、Python doctor 和 Python Web 启动器；仓库运行栈改为 TypeScript + JavaScript + HTML + CSS。
- **启动方式切换为 Node** — Web 服务使用 `npm start` / `start.sh` / `start.bat` 启动，CLI 使用 `npm run tinda`，doctor 使用 `npm run doctor`；旧的 Python 模块导入路径不再存在。

### Added

- **TypeScript Web 后端** — 新增 Express Web 服务，覆盖首页、Chat、SSE、认证、会话、日志、模型数据、终端事件和系统版本等现有前端 API。
- **TypeScript Agent 与工具运行时** — 新增 OpenAI-compatible LLM client、工具调用循环、内置工具注册表、文件读写/搜索、Web 搜索、异步终端任务和终端事件轮询。
- **TypeScript 会话兼容层** — 新增 JSON session store 和 adapter，继续读写 `~/.tinda/agent/Data/Sessions` 下的会话数据格式。
- **Node doctor/CLI** — 新增 `src/cli/doctor.ts` 与 `src/cli/main.ts`，替代旧 Python doctor 和 CLI 主路径。

### Changed

- **版本提升** — 项目版本从 `1.10.1` 提升到 `1.11.0`，同步更新 `package.json`、`package-lock.json`、Web 版本徽章、Chat/Settings/HOME fallback 和主题脚本标识。
- **文档切换为 TS-only** — README、架构文档和开发策略全部更新为 TypeScript-only 运行时说明。
- **端口管理切换为 Node 服务内建** — `src/web/server.ts` 内置端口重试和 `.tinda_ports.list` 追踪，`status.*` / `stop.*` 只识别 Node/TypeScript 服务。

### Removed

- **旧 Python 兼容入口** — 移除 `run_web.py`、`doctor.py`、Python package metadata、Python 单测和 Python-only helper scripts。
- **旧 Python 后端模块** — 移除 `TindaAgent/CLI`、`Process` Python 实现、`Tool` Python 实现、`User` Python 实现、`Web/*.py` 后端实现和 legacy `Master`/`Tests` Python 测试。

### Defense

- **构建验证** — `npm test` 执行 TypeScript 编译检查。
- **运行诊断** — `npm run doctor` 验证 Node、运行时目录、用户数据、日志目录、API key 配置和构建产物。
- **HTTP smoke** — 通过临时端口验证 `/app`、`/auth/local-login`、`/sessions`、`/sessions/{id}/messages` 和 `/model`。

## v1.10.1 - 2026-05-24

### Changed

- **版本提升** — 项目版本从 `1.10.0` 提升到 `1.10.1`，同步更新 `pyproject.toml`、HOME/Chat/Settings fallback 和主题脚本标识。
- **Plan 协议简化** — 移除模型可见的计划完成确认动作，`plan` 公开 action 收敛为 `create/update/set_step_status/block/clear`；计划完成直接使用 `status=complete` / `completed=true` 表达。
- **Plan UI 状态简化** — 前端不再显示“待用户确认完成 / 已确认完成”，计划完成态统一展示为“已完成”，避免把计划记录误设计成用户确认工作流。

### Fixed

- **Plan `steps` 参数解析** — 修复 LLM 工具边界把 `steps` 数组强制转成字符串后被校验拒绝的问题；`run_agent_tool` 现在会为 `Any/list/dict` 参数保留结构化 JSON 值。
- **Plan `steps` 兼容兜底** — `steps` 解析支持原生数组、JSON 字符串和历史 Python repr 字符串，同时继续拒绝换行/分号拼接的非结构化步骤文本。
- **Plan 状态持久化** — 会话计划状态写入独立 plan 文件并随会话消息接口返回，刷新或切换会话后可恢复当前计划，同时保留关闭/删除计划的状态。

## v1.10.0 - 2026-05-23

### Added

- **Codex/Claude Code 风格文件编辑工具** — 新增 `read_file` / `edit_file`，支持 UTF-8 文本读取、唯一 `old_text` 精确替换、`expected_sha256` 并发保护、`dry_run` diff、新文件创建和写入前校验。
- **文件搜索工具** — 新增 `search_files`，支持按 root、文件名/路径子串、文本内容、glob 与结果上限搜索，返回路径、行号和 snippet，便于 LLM 编辑前定位目标文件。
- **本地 Web 搜索工具** — 新增 `search_web`，按 Tavily API、DuckDuckGo HTML、内置站点索引三层降级，支持 answer/raw_content、site 限定、结果数量控制和离线索引兜底。
- **常用站点索引表** — 内置搜索引擎、开发者文档、社区、AI 文档、包仓库、GitHub、arXiv 等常用直达入口，离线时仍能给 LLM 返回结构化链接。
- **MCP stdio 桥接** — 新增 `mcp_add_server`、`mcp_list_servers`、`mcp_list_tools`、`mcp_call_tool`，提供最小 MCP JSON-RPC 初始化、工具列表和工具调用链路。
- **本地 Skill 系统** — 新增 `skill_list` / `skill_read`，从 `~/.tinda/agent/skills/{name}/SKILL.md` 与 `TINDA_SKILL_PATHS` 发现并按需读取技能，避免技能全文常驻上下文。
- **用户澄清提问工具** — 新增 `ask_user_question` 原生工具，支持选择题、填空、自定义补充和“以上都不是，我自己补充”，LLM 可暂停当前轮等待用户补充条件。
- **Plan 工具** — 新增 `plan` 原生工具，用于记录目标、步骤、状态和备注；Plan 模式下只允许 `ask_user_question` 与 `plan`，阻止执行型工具误跑。
- **Deep 对齐模式** — Chat 输入栏新增 `Deep` 开关，发送前先生成用户可确认的理解摘要；用户可确认、补充、返回上一版、取消或折叠查看历史轮次。
- **Deep 对齐持久化** — 未确认 Deep 对齐状态保存到 `Data/DeepAlignment`，刷新或服务重启后可恢复原请求、附件、轮次、当前页和待回答问题。
- **Deep 内嵌澄清交互** — Deep 模式兼容 `ask_user_question`，可在理解摘要前弹出卡片内问题，用户回答后继续生成新的对齐摘要。
- **Plan 可移动便签 UI** — Chat 新增可拖拽、折叠、关闭的玻璃便签窗口，用于展示 `plan` 工具生成的目标、步骤、状态和备注。
- **输入框工具选择行** — Chat 输入区新增 `+` 工具选择入口和已选工具 chip，支持把 Web Search 显式注入当前请求，避免默认污染所有请求上下文。
- **模型数据面板调用参数区** — 新增可编辑 `temperature`、`top_p`、presence/frequency penalty、`max_tokens`、`seed`、`timeout`、`tool_choice`、`max_tool_steps`、Thinking、Reasoning Effort 等参数。
- **模型数据 hover 说明** — provider、模型、余额、请求摘要和调用参数字段增加悬浮提示，截断字段可查看完整值和用途。
- **请求体与余额面板扩展** — 模型数据面板继续展示真实 SDK 请求体、provider、模型、payload 字符、上下文字符、token 明细、DeepSeek 余额和最近请求统计。
- **工具权限声明补齐** — `tool_min_permissions.json` 新增 `ask_user_question`、`plan`、文件编辑、文件搜索、Web 搜索、MCP、Skill 等工具的最低权限和说明。
- **上下文压缩模块** — 新增 `context_compaction.py`，集中处理 Markdown、终端上下文、tool result JSON、assistant/tool message 的请求前压缩。
- **前端 Markdown 渲染能力** — Markdown renderer 支持宽松管道表格、标准表格、BBCode 风格 `[code]...[/code]` / `[code=lang]...[/code]` 和受控 `toolskip:` 链接。
- **自动化回归用例扩展** — 补充工具、Deep、Plan、Web 搜索、MCP/Skill、Markdown、provider 参数、reasoning_content、工具跳过和澄清取消等测试。

### Changed

- **版本提升** — 项目版本从 `1.9.0` 提升到 `1.10.0`，同步更新 `pyproject.toml`、Web 版本徽章、Chat/Settings fallback 和主题脚本标识。
- **LLM 调度 provider-aware** — `LLMClient` 与 `LlmDispatcher` 增加 provider 透传，请求清洗、DeepSeek 兼容、OpenAI-compatible 调用和模型检测统一走调度层。
- **请求边界清洗统一** — 新增 `prepare_llm_request_payload()`，所有真实发送和记录的请求体在边界统一处理 provider/model 特定字段。
- **DeepSeek V4 thinking 语义调整** — DeepSeek V4 thinking 场景保留带 `tool_calls` assistant 的 `reasoning_content`；非工具 assistant 推理内容不再进入后续请求。
- **旧 reasoner 兼容策略** — `deepseek-reasoner` 继续作为旧模型 ID 兼容分支，请求前剥离 `reasoning_content`，避免 400。
- **非 DeepSeek provider 隔离** — OpenAI/Google/Anthropic/自定义 provider 不接收 DeepSeek 专属 `reasoning_content` 字段，降低跨 provider 请求失败概率。
- **系统提示词英文化** — Deep、Plan、记忆策略、工具模式等 LLM 注入提示调整为英文，用户可见 UI 仍保留中文。
- **Deep 与 Plan 职责分离** — Deep 只做意图对齐和澄清，不制定执行计划；Plan 模式负责计划记录，不执行任务工具。
- **ask_user_question 提示词强化** — `ask_user_question` 的工具 schema 描述和参数说明改为英文强约束，要求 LLM 只问一个阻塞问题、等待工具结果、不模拟用户回答、不在 pending 状态继续执行。
- **Plan 模式工具拦截** — Plan 模式下即使工具 schema 可见，执行型工具也会被后端返回 `plan_mode_execution_blocked`，防止模型绕过计划模式。
- **Web Search 显式启用** — `search_web` 只有当前请求带 `[WEB_SEARCH_MODE]` 时可执行，未启用时返回 `web_search_disabled`，减少工具注入和缓存前缀抖动。
- **工具轮回改为模型主导** — 只要模型返回 `tool_calls` 就执行并回填结果；模型不再请求工具才结束，不再提前关闭工具。
- **工具上限语义重算** — `max_tool_steps` 表示完整可执行工具轮数；达到上限后注入最终总结系统消息，并强制 `tool_choice=none`。
- **工具上限回复兜底** — 如果模型把内部上限提示原样返回，前端/后端会替换为用户可读的中文上限说明。
- **工具结果请求前压缩** — tool result 写入 LLM request 前会压缩 `stdout/output`、去重 `ok/success`、稳定 JSON key，降低长工具输出 token 压力。
- **上下文请求前压缩** — Markdown 展示性符号、代码围栏、终端 ANSI、超长终端输出和工具 JSON 在进入 LLM 前被压缩，存储层和前端展示仍保留原结构。
- **记忆上下文插入顺序调整** — memory/transient system context 插入到最后一条 user 前，保持当前用户请求后缀稳定。
- **Chat 发送链路默认流式** — Chat 发送优先使用流式链路，前端按节流渲染流式文本、reasoning 和 tool marker，减少长回复卡顿。
- **输入框高度自适应** — Composer 随输入增长并同步消息区底部 padding，避免最后一条消息被增高的输入框遮挡。
- **Chat 快捷栏配置化** — 右上角功能入口按用户设置和权限动态渲染，模型数据、日志、用户管理、会话管理、终端等入口统一图标按钮。
- **账号切换菜单玻璃化** — Chat 左上角用户切换菜单沿用登录用户选择界面的透明玻璃视觉，降低视觉割裂。
- **HOME/Chat/Settings 版本显示同步** — HOME、Chat、Settings 的 fallback 文案全部改为 `v1.10.0`，运行时版本仍以 `/system/version` 为准。

### Fixed

- **ask_user_question 取消状态丢失** — 修复普通 assistant 澄清弹窗复用终端确认接口时后端强制 `approval=True` 的问题；取消现在会以 `approval:false` / `action:deny` 回填给 LLM。
- **ask_user_question 选项污染** — 修复 LLM 把“（这是一个选项）A/B/C...”写进 options 时前端第一项带说明性前缀的问题；工具提示要求一项一行，后端兼容拆分并清理说明前缀。
- **ask_user_question 锁定文案** — 修复澄清问题 pending 时输入框仍提示“存在待确认终端命令”的问题；前端锁定态现在按 question/terminal 显示对应文案。
- **澄清回答后的错误系统提示** — 修复 `ask_user_question` 回答后仍注入“终端命令已执行”的系统提示，导致 LLM 不继续使用工具的问题；现在按回答/取消分别注入澄清语义。
- **Deep ask 交互不可继续** — 修复 Deep 卡片内 `ask_user_question` 回答/取消后未正确恢复 Deep 对齐状态的问题。
- **Deep 请求上下文缺失** — 修复 Deep 模式只携带当前用户原文、未带入上文有效上下文，导致“好了/继续/就这样”被孤立解析的问题。
- **Deep 与 Plan 混淆** — 修复 Deep 阶段模型因为实际工具列表受限而声称“没有 plan 工具”的问题；Deep 只暴露澄清工具，但会告知确认后主运行时可能有更多工具。
- **Plan 工具不可见** — 修复 `/plan` 语义只靠文本提示、没有原生 `plan` tool marker 展示的问题。
- **工具调用覆盖 thinking/正文** — 修复 DSML/tool_calls 流式替换时覆盖已输出 thinking、正文或前序 tool marker 的问题。
- **tool marker 合并/乱序** — 修复多个工具调用时 start/done marker 被合并、完成提示跑到准备提示前、刷新后 marker 状态分裂的问题。
- **tool marker 保真** — 修复工具 marker 字段被改写导致 `stdout`、`arguments`、`result`、`tool_call_id` 不稳定的问题，继续按存储顺序续写和渲染。
- **reasoning_content 400** — 修复 DeepSeek 请求中不该回传 `reasoning_content` 的轮次导致 `invalid_request_error` 的问题。
- **工具调用 assistant reasoning 缺失** — 修复 DeepSeek thinking + tool_calls 场景下后续请求缺失必需 `reasoning_content` 的问题。
- **工具上限提前触发** — 修复 `max_tool_steps` 在真实工具轮数未达到时提前禁用工具、四轮左右就停止的问题。
- **工具上限提示污染气泡** — 修复内部 “Maximum tool call iterations reached” 文本直接出现在 assistant 气泡里的问题。
- **工具跳过假生效** — 修复 skip 只更新前端、后端工具仍等待的问题；跳过会写入 `user_skipped` tool result 并让 LLM 继续总结或换方案。
- **长工具调用无反馈** — 修复长搜索/终端执行时前端像卡死的问题，heartbeat 会显示“连接中 / 执行中 · 已等待 Ns”。
- **后台 late result 覆盖** — 修复跳过工具后后台线程返回结果覆盖前端/会话状态的问题。
- **`/sessions/{sid}/context-usage` 404** — 修复真实 session 已创建但 meta 尚未落盘时 context-usage 返回 404 的竞态。
- **工具事件 403 刷屏** — 修复切换账户、删除会话或不可访问会话时 `/tool-events` 持续 403 刷日志的问题。
- **会话删除/切换残留** — 修复删除当前会话或新建草稿后旧消息 DOM、终端回放、分页状态和 pending confirm 残留的问题。
- **新会话草稿逻辑** — 修复进入 Chat 或点击新建就创建空会话文件的问题；只有真正发送用户消息/附件时才创建真实会话并出现在列表。
- **消息底部遮挡** — 修复输入框增高后消息区底部 padding 不足，最后消息被挡住的问题。
- **流式工具后一次性输出** — 优化工具调用后的流式恢复与前端节流渲染，降低工具后回复一次性刷出的概率。
- **Markdown 宽松表格** — 修复 LLM 输出 `源 | 条件 | 方式` 这种无分隔行表格时不渲染的问题。
- **BBCode 代码块** — 修复 `[code]...[/code]` 和 `[code=lang]...[/code]` 在气泡中原样显示的问题。
- **Plan marker 渲染** — `plan` 工具结果现在渲染为计划便签/计划 marker，而不是普通工具 JSON。
- **Deep 卡片翻页** — 修复 Deep 确认后仍需手动翻页才能看到最新理解的问题。
- **Deep 状态清理** — 删除会话、reset 或确认/取消后会清理 Deep alignment 持久化状态，避免刷新恢复过期卡片。
- **模型参数写入不完整** — 修复模型数据面板展示了参数但未完整写入后续真实请求的问题。
- **请求日志与真实 body 偏移** — 请求日志记录经过 provider 清洗后的真实 SDK body，减少模型数据面板与实际请求不一致。
- **前端暗色按钮/Deep 按钮状态** — 补齐 Deep 按钮在暗色模式、hover、active 状态下的玻璃层级。
- **账号切换浮层视觉割裂** — 用户切换弹窗与登录选择界面统一透明玻璃样式。

### Defense

- **Deep/Plan 回归** — 覆盖 Deep 英文系统提示、Deep 上下文带入、Deep ask 恢复、Plan 前缀剥离、Plan tool marker 和 Plan 模式工具拦截。
- **DeepSeek 请求兼容回归** — 覆盖 V4 thinking 带工具 assistant 保留 `reasoning_content`、非工具 assistant 剥离、旧 reasoner 剥离和非 DeepSeek provider 剥离。
- **工具轮回回归** — 覆盖工具上限完整执行、超过上限后 finalize、内部提示不外露、运行中 skip 立即返回、skip alias 命中和 provider 参数 900 上限。
- **Web Search 回归** — 覆盖 Tavily、有网 DuckDuckGo、离线内置索引、工具权限注册和 disabled 状态。
- **文件/MCP/Skill 回归** — 覆盖编辑唯一替换、dry-run diff、文件搜索内容定位、MCP/Skill 工具注册和权限表。
- **Markdown/前端回归** — 覆盖宽松表格、BBCode 代码块、Deep/ask UI、Plan 浮窗、输入框工具选择和 toolskip 安全链接。
- **会话/上下文回归** — 覆盖 context-usage live session 兜底、请求顺序、工具/终端上下文回放、压缩展示和 ask_user_question 取消状态。

## v1.9.0 - 2026-05-20

### Added

- **Codex/Claude Code 风格编辑工具** — 新增 `read_file` / `edit_file` 原生工具，支持 UTF-8 文本读取、唯一 old_text 精确替换、`expected_sha256` 并发保护、`dry_run` diff 和新文件创建。
- **文件搜索工具** — 新增 `search_files`，支持按 root、文件名/路径子串、文本内容和 glob 搜索，返回有限数量的路径、行号和 snippet，便于编辑前定位文件且避免大输出污染上下文。
- **MCP stdio 桥接工具** — 新增 `mcp_add_server`、`mcp_list_servers`、`mcp_list_tools`、`mcp_call_tool`，按 MCP JSON-RPC `initialize`、`tools/list`、`tools/call` 最小链路接入本地 stdio MCP server。
- **本地 Skill 系统** — 新增 `skill_list` / `skill_read`，从 `~/.tinda/agent/skills/{name}/SKILL.md` 与 `TINDA_SKILL_PATHS` 发现并按需读取技能说明，不把技能内容常驻注入上下文。
- **Agent 集群工具跳过控制** — Chat 工具调用气泡在运行中显示 `跳过` 操作，长时间搜索、终端命令或工具执行可由用户主动中止，不再只能等待工具自然返回。
- **工具跳过后端接口** — 新增 `POST /sessions/{sid}/tool-calls/{tool_call_id}/skip`，前端 `toolskip:` 动作统一转发到后端，由当前会话的 Agent/LLM client 消费跳过请求。
- **工具执行进程可取消** — `run_terminal` 从阻塞式 `subprocess.run()` 调整为 `subprocess.Popen()` 执行，工具 call_id 与运行中进程绑定；用户跳过时向进程发送终止信号并返回结构化 `user_skipped` 结果。
- **会话级工具调度上下文** — Agent、LLMClient 与 MultiProviderToolClient 支持透传 `session_id`，工具跳过、heartbeat 和工具执行状态可按会话隔离，避免不同会话工具状态串扰。
- **模型调用参数面板** — 模型数据面板新增可编辑调用参数区，支持 `temperature`、`top_p`、presence/frequency penalty、`max_tokens`、`seed`、`timeout`、`tool_choice`、`max_tool_steps`、Thinking 与 Reasoning Effort，保存后写入 provider 配置并影响后续真实 LLM 请求。
- **模型数据悬浮提示** — 供应商标签、provider 配置项、模型胶囊、调用参数、余额明细和最近请求统计项增加 hover title，截断名称可查看完整值与字段作用。
- **网络搜索工具** — 新增 `search_web` 原生工具，`TAVILY_API_KEY` 存在时优先走 Tavily Search API，无 key、Tavily 失败或强制 `source=builtin/index` 时使用内置 DuckDuckGo HTML 解析与常用站点索引兜底。
- **常用网络索引表** — 内置 Codex-like 搜索目标索引，覆盖通用搜索、GitHub/Stack Overflow/Reddit/Hacker News、官方开发文档、包仓库、AI API 文档、arXiv 等常用入口，供 `search_web(source=index)` 或兜底搜索返回结构化链接。
- **用户澄清提问工具** — 新增 `ask_user_question` 原生工具，LLM 可在缺少关键条件时暂停当前轮并向用户展示可选项与补充输入；有选项时自动追加“以上都不是，我自己补充”，用户回答后以 tool result 回填给 LLM 继续执行，交互形态接近 Claude Code 的 clarify flow。
- **Deep 对齐确认循环** — Chat 输入栏新增 `Deep` 独立开关；开启后用户发送消息会先生成“理解确认”交互块，用户可选择“一致继续执行”、补充说明后重新对齐、返回上一级理解或取消，直到确认后才复用原始 Chat 执行链路。
- **Deep 对齐状态持久化** — 未确认的 Deep 对齐轮次保存到运行时 `Data/DeepAlignment` 独立 JSON 文件，刷新页面或服务重启后可恢复原用户气泡、附件 chip 与确认卡片；确认、取消、删除会话或 reset 时自动清理。

### Changed

- **上下文输入压缩策略** — LLM 请求新增统一 context compaction 层：会话文件和前端继续保留 Markdown/工具详情，真实写入模型的历史 assistant/system/terminal/tool 内容会去除展示型 Markdown、压缩代码围栏、稳定 JSON key、去重 `stdout/output` 与 `ok/success`，减少“输入 token 比缓存收益还多”的情况，并保持稳定前缀优先命中缓存。
- **版本提升** — 项目版本从 `1.8.3` 提升到 `1.9.0`，同步更新 Web 版本徽章、设置页 fallback、Chat fallback 和主题脚本标识。
- **长工具调用交互策略** — 长工具调用不再依赖固定超时或前端拦截，改为“heartbeat/progress 持续反馈 + 用户显式跳过”的控制模型，保留长任务能力同时给用户可见进度和退出手段。
- **工具循环跳过收束** — LLM 工具循环消费到 `user_skipped` 后会优雅结束当前工具链路，并向会话写入“工具调用已被用户跳过”的 assistant fallback，避免继续等待已取消工具。
- **标准 Agent 工具轮回** — 工具循环改为模型主导：LLM 返回 `tool_calls` 就执行工具并回填结果，LLM 不再返回 `tool_calls` 才结束；不再用 `max_tool_steps - 1` 提前禁工具。
- **工具上限语义调整** — `max_tool_steps` 表示可完整执行的工具轮次；只有执行满上限后模型仍继续请求工具时，后端才注入上限说明并以 `tool_choice=none` 做最终总结。
- **Deep 对齐执行策略** — Deep 对齐走发送前拦截，不改动现有 `/chat`、`/chat/stream`、工具循环、tool marker 和会话存储主链路；确认后原始用户请求按原文进入会话，最终对齐摘要只作为本轮隐藏 system 上下文注入 LLM 请求。
- **Deep 对齐上下文修正** — Deep alignment 请求会带入当前会话的有效 LLM 上下文最近消息，再结合当前用户请求生成理解确认，避免用户输入“好了/继续/就这样”这类承接上文时被错误地孤立解析。
- **工具轮次上限放宽** — provider 配置、OpenAI-compatible client、dispatcher 与模型数据面板统一支持 `max_tool_steps` 范围 `1~900`，避免 900 被前端或后端截断为 20/6。
- **Provider 参数贯通** — Web Chat 主链路默认读取 provider 级调用参数；DeepSeek 默认保留 Thinking enabled 与 Reasoning max，CLI 显式温度调用保持兼容。

### Fixed

- **context-usage 首轮 404 竞态** — 修复真实 session 刚创建但 meta 尚未落盘时 `/sessions/{sid}/context-usage` 返回 404 的问题；live agent 存在时后端会兜底返回空 usage，前端首轮创建阶段也会跳过过早轮询。
- **工具跳过 ID 映射** — 工具模型侧 `tool_call_id` 与内部审计 `call_id` 建立 alias 绑定，前端跳过动作可在工具开始前、执行中或 ID 刚生成后正确命中同一次工具调用。
- **工具跳过渲染安全** — Markdown 渲染器允许 `toolskip:` 作为受控安全链接，由 document click 统一拦截处理，避免把跳过动作当普通外链或危险协议渲染。
- **运行中工具跳过假生效** — 修复 skip 只更新前端状态、不打断后端工具等待的问题；流式工具 runner 现在会立即写入 `user_skipped` tool result，让 LLM 收到跳过事实后继续换方案或总结。
- **跳过后后台结果覆盖** — 跳过执行中的工具后，后台线程返回的 late result 会被标记废弃，不再覆盖会话消息、工具 trace 或继续执行同一轮剩余工具。
- **工具上限提前收工** — 修复工具循环在接近上限时提前关闭工具的问题；`max_tool_steps=1` 也会完整执行一轮工具，只有下一轮模型继续请求工具时才触发上限收束。
- **请求摘要参数缺失** — 最近一次真实 SDK 请求摘要补充展示 `top_p`、`max_tokens`、Thinking 类型和 Reasoning Effort，便于核对模型参数是否真实写入。
- **自动化覆盖** — 增加 context-usage live session 兜底测试和 tool skip 消费测试，并补充长终端跳过 smoke 验证，覆盖本轮 Agent 集群工具控制链路。
- **工具循环自动化覆盖** — 增加运行中 skip 立即返回、provider 参数 900 上限保留、工具预算完整使用后再 finalize 的回归测试。
- **Markdown 宽松表格渲染** — 前端 Markdown 渲染器支持 LLM 常见的无分隔行管道表格，如 `源 | 条件 | 方式` 后直接跟数据行，避免工具/网络搜索总结在 Chat 气泡里退化成普通文本。
- **BBCode 风格代码块渲染** — 前端 Markdown 渲染器兼容 `[code]...[/code]` 与 `[code=lang]...[/code]`，LLM 输出流程图/日志片段时会渲染为代码块而不是普通文本。

## v1.8.3 - 2026-05-17

### Added

- **LLM 请求体查看页** — 新增 `/llm-request` 页面与 `/llm-request/latest` 接口，可直接查看最近一次真实写入 DeepSeek/OpenAI 兼容 SDK 的请求体、模型、消息数、工具数、payload 字符数、内容字符数与估算 token。
- **模型数据面板** — `/llm-request` 扩展并别名为 `/model-data`，新增 DeepSeek 账户余额查询、余额卡片入退场动画、供应商 tab、请求体摘要、真实 payload、token 明细、缓存命中相关字段与模型数据总览。
- **模型供应商管理** — 新增 `/model-data/providers`、`/model-data/models`、`/model-data/balance` 接口和前端管理表单，支持 DeepSeek 默认供应商，以及 OpenAI、Google Gemini、Anthropic 和自定义 OpenAI-compatible provider 的 `base_url`、调用路径、API Key 环境变量、模型 ID/显示名配置。
- **LLM 调度抽象层** — 新增 `Process/AI/dispatcher.py` 与 `providers.py`，引入 provider-aware LLM dispatcher，集中管理当前 provider/model、辅助模型、DeepSeek 余额、模型检测调用链路和跨供应商请求日志。
- **HOME 卡槽化左右面板** — HOME 左右区域改为可组合卡槽结构，左侧 changelog、右侧运行状态/余额/模型数据等内容由 slot 渲染，便于后续自定义显示内容。
- **HOME 真实运行数据面板** — HOME 右侧状态接入真实系统检测，展示启动时间、系统时间、系统内存、进程内存、负载、存储卷、24h 使用柱状图、日历热力图与真实 runtime 统计。
- **HOME 存储卷选择 UI** — 存储 donut 增加合并标签式卷选择器，支持按 C/D/E 等挂载卷切换，切换时图表从当前值平滑过渡到新值。
- **统一 HOME 动画编排** — 新增 `data-home-motion` / `data-home-exit` 卡槽动画编排，卡片只声明 UI，入场/退场由统一 motion pipeline 处理，支持左/右卡片从上到下逐项淡入滑入和统一退场。
- **会话分页与 SQLite 读索引** — 新增非权威 SQLite session index，用作长会话读取缓存；`GET /sessions/{id}/messages` 支持 `limit`、`before_seq`，前端增加“加载更早消息”，初始仅加载最近消息以降低长会话渲染压力。
- **HOME 极致动画打磨** — HOME 三栏布局补齐更完整的分层动效：左侧更新日志卡片弹入、标题/内容从上方落下、Markdown 文本从上往下逐项淡入；中间主卡保留玻璃卡片入场；右侧运行状态卡片、统计块、热力图、24h 柱状图、内存/存储 donut、启动时间与系统时间按从上到下的顺序错峰入场。
- **HOME 页面退场动画** — 从 HOME 跳转到聊天、用户管理或日志页时不再硬切：顶部栏上移淡出，左/中/右三张卡片按各自方向退场，页面整体柔和淡出后再导航。
- **Chat 退场终端联动** — Chat 页面退场前主动收起终端、保存终端宽度、清理拖拽状态，并同步关闭时间、模型、会话浮层，避免跳转退场画面残留终端面板。
- **Chat 分层入退场打磨** — Chat 顶部品牌区、版本号、用户切换、右侧功能栏、状态栏、消息区、输入框、发送/附件/时间按钮和底部提示统一为方向感入场；离开页面时按 header、状态栏、输入区、消息区分层退场。
- **全站主题切换按钮** — 新增共享 `theme_toggle.js`，HOME、Chat、Settings、Logs、User Admin、Model Diagnostics 同步浅色/深色状态；主题按钮增加旋转、缩放、图标切换动画，并在 CSS 解析前写入 `data-theme` 防止闪白。
- **用户管理交互动画** — 用户管理页保留账户列表逐项入场，按钮、权限项、toast、表单输入、卡片 hover 统一拆分 transition，不再使用粗粒度 `transition: all`。
- **日志与模型诊断动效对齐** — 日志页、模型诊断页补齐深色夜樱玻璃配色和按钮 hover/active 过渡；按钮图标、行高、居中方式统一，避免交互时视觉跳动。
- **Settings 外观入口** — 设置页新增外观卡片和主题切换控件；设置页 header、按钮、版本区域和卡片动效与 HOME/Chat 的玻璃风格保持一致。
- **视觉连续性细节** — HOME 左侧 changelog 禁止横向滑动，代码块和表格自动换行；右侧状态面板增加块内图表/文字级别的淡入，提升动画层次感。

### Changed

- **版本提升** — 项目版本从 `1.8.2` 提升到 `1.8.3`，同步更新 Web 版本徽章、设置页版本 fallback、Chat 版本 fallback 和主题脚本标识。
- **LLM 请求体组装顺序稳定化** — LLM 请求改为更利于缓存命中的稳定结构：固定英文 system policy 始终位于最前；工具 schema 在同权限下固定排序并缓存；memory 上下文延后到历史消息之后、当前用户消息之前；终端上下文按时间顺序合并到 messages 尾部附近。
- **LLM 请求日志精确化** — 请求日志继续记录真实 SDK request body，同时在收到官方 usage 后回写 prompt/completion/total token；无 usage 时使用 DeepSeek 官方 tokenizer 文件计数，避免状态栏与请求体面板口径偏移。
- **模型检测链路 provider-aware** — 模型检测页面改为按 provider/model 选择调用链路，DeepSeek 继续走 OpenAI-compatible，Google/Anthropic 可使用专属 adapter payload。
- **DeepSeek thinking 配置** — DeepSeek 请求统一携带 `thinking: {type: enabled}` 与 `reasoning_effort: max`，保持推理模式开关和强度配置一致。
- **上下文阈值标准化** — 上下文 token 阈值统一限制为 `16K ~ 200K`；设置页、Chat 配置弹窗、后端 settings 校验、会话 config 和运行中 agent 同步使用同一标准。
- **自动压缩触发点调整** — 自动上下文压缩改为 LLM 请求完成后按真实上下文 token 阈值判断，不再按原始消息条数触发；压缩结果作为 system substep 附加到当前 assistant turn。
- **上下文状态栏口径调整** — Chat 状态栏展示 `当前上下文/阈值`，压缩发生时展示 `压缩前→压缩后/阈值`，只统计实际进入 LLM request context 的消息。
- **终端上下文独立回放** — 终端历史改为独立读取 `/sessions/{id}/terminal` 并重放到终端面板，聊天消息和终端事件在前端渲染层分离。
- **run_terminal 长任务策略** — 移除 `run_terminal` 对命令执行的固定 subprocess timeout，长命令不再被硬超时截断，改由 SSE heartbeat/progress 持续反馈连接状态。
- **README 路由说明更新** — README 将 LLM 请求页说明升级为模型数据面板，补充 `/model-data`、`/model-data/latest`、`/model-data/balance` 路由说明。
- **依赖更新** — `pyproject.toml` 新增 `tokenizers>=0.22` 与 `jinja2>=3.1.0`，用于官方 tokenizer 与 Web 模板/面板能力。
- **Web 动效策略** — 关键页面动画从单纯淡入扩展为“卡片级 + 内容级 + 文本级”的组合序列；按钮/卡片/表单过渡改为显式列出 `transform`、`opacity`、`background`、`border-color`、`box-shadow` 等属性，减少布局抖动；在 `prefers-reduced-motion` 下仍遵循已有降级策略。
- **新建会话草稿化** — Chat 进入页与会话管理“新建”只创建前端草稿态，不再立即生成会话文件或出现在会话列表；只有真正发送用户消息/文件时才分配真实会话并写入列表，符合“有用户输入才有会话”的产品直觉。
- **会话列表只展示有效会话** — 会话列表默认过滤 `message_count=0` 的空会话；只读接口不再因为读取消息、上下文用量、终端或工具事件而隐式创建会话。

### Fixed

- **请求体展示与真实 API body 对齐** — 请求体日志会自动剥离 `timeout` 之类 SDK-only 字段，只保留真实写入 API body 的 `messages/tools/thinking/...`；模型检测等直连 SDK 调用也会统一落日志。
- **工具 marker 实时顺序** — Chat 流式工具 marker 从字符串替换改为结构化顺序队列，`tool_call_start` 插入“准备调用”，`tool_step` 原地升级同一块，避免多个工具时合并、乱序或完成提示跑到前面。
- **工具执行 progress** — 流式工具执行期间每秒发送 `tool_heartbeat`，前端同一个 `tool_marker` 显示“连接中 / 执行中 · 已等待 Ns”，长工具调用不再像卡死。
- **工具调用期间前文覆盖** — 修复 `replace_segment` 与 reasoning flush 边界，工具调用协议/DSML 被替换时不再覆盖已输出的 thinking、正文或前序工具 marker。
- **工具 marker 持久化匹配** — `append_to_assistant_by_turn` 增强 `id/tool_call_id/name+stdin` 匹配，running marker 会被 done marker 替换，不再刷新后出现 start/done 分裂。
- **tool_marker 字段保真** — 工具 marker 继续保留 `name/id/tool_call_id/status/arguments/result/stdout/stdin`，前端渲染与会话文件顺序续写，不再改写成不兼容结构。
- **长工具调用断链体验** — 后端工具执行改为 worker + heartbeat，SSE 连接在同步工具执行期间持续有事件输出，降低代理/浏览器空闲断连概率。
- **context-usage 404 自愈** — `_require_session_access(create=False)` 在消息文件存在但 session meta 暂缺时自动补元数据，避免有效会话的 `/context-usage` 返回 404。
- **会话读取性能** — Chat 初始加载和切换会话只渲染最近消息，旧消息按页加载；SQLite index 只作缓存，JSON 仍为权威数据，删除/压缩/写入时自动失效缓存。
- **会话删除/切换残留** — 删除当前会话、删除全部会话或切换会话后清理旧 DOM、终端回放状态、分页状态和 pending confirm，避免旧内容残留到新草稿。
- **上下文顺序修正** — LLM 请求体按会话 JSON sequence 顺序写入，不再因时间戳或终端记录导致前后文错序。
- **系统/终端消息上下文过滤** — `display_target` 与 `context_policy` 统一标记 chat/terminal/system/summary 事件，UI 通知不进入 LLM 上下文，summary/terminal context 才按策略进入。
- **上下文压缩展示** — 自动压缩结果附加到 assistant turn 的 system substep，前端按普通 markdown 渲染引用内容，不再跳转到欢迎页或丢失压缩前消息。
- **状态栏 token 阈值同步** — 设置页保存阈值会同步 `_session_config` 和运行中 agent，Chat 弹窗也会 PATCH 当前 session config，避免前端阈值与后端判断不一致。
- **DeepSeek token 计数日志噪音** — 官方 tokenizer 优先使用本地 tokenizer 文件计数，不依赖 PyTorch/TensorFlow/Flax 模型加载，降低 tokenizer 初始化误解和日志噪音。
- **HOME 右侧性能数据真实性** — 右侧性能面板改为读取真实系统内存、进程内存、负载、运行时长、系统时间和磁盘卷，不再显示固定 100G 之类占位数据。
- **HOME 图表动画卡顿** — donut、存储卷切换、热力图、24h 柱状图使用数值缓动和未完成动画续接，快速切换时不会瞬移或卡顿。
- **模型余额面板对齐** — DeepSeek 余额右侧四项数据重排为对称卡片，数值字号、换行、高度和入退场动画统一，减少面板视觉错乱。
- **暗色/玻璃风一致性** — Chat 暗色模式配色调整为与 HOME 的深夜樱粉玻璃方向一致，AI 气泡、用户气泡、系统提示、thinking 低饱和灰粉层级统一。
- **工具与终端上下文排序** — 历史 `tool` 消息、`tool_calls`、独立终端日志现在能稳定按时间顺序并入 LLM 上下文，避免回灌时丢掉工具结果或把终端上下文排错位置。
- **DeepSeek DSML/tool_calls 渲染链路** — 流式 DSML 工具调用转为安全 tool marker 顺序写入，前端使用 `replace_segment` 覆盖临时 DSML，不再污染会话内容或覆盖 thinking。
- **工具调用断链持久化** — 流式请求开始后立即写入会话草稿，工具开始/完成 marker 按 turn_id 增量更新，降低刷新或长链路中断时丢失工具标记的概率。
- **run_terminal 多行脚本执行** — 终端工具保留换行并优先使用 bash 执行，非零退出码统一返回失败状态和错误信息，修复 heredoc/多行 Python 脚本执行异常。
- **工具事件轮询 403 刷屏** — 当前会话不可访问、被删除或切换账户后自动停止工具事件轮询，避免 `/tool-events` 持续 403 刷日志。
- **会话管理状态机** — 切换/新建/删除会话统一复用 Chat 加载层并先清旧 DOM；删除当前或全部会话后保持 LOGO 空态，不再自动跳到其它会话或残留旧消息。
- **终端分隔线自适应** — 终端气泡内分隔线从固定 `─` 文本改为 CSS 自适应线，窄终端面板下不再折成多行。
- **面板按钮换行** — 会话管理顶部按钮组、用户管理顶部操作、创建账户按钮和表格操作按钮改为单行横向滑动，避免中文按钮在宽度不足时被强制换行。

## v1.8.2 - 2026-05-13

> 2026-05-16 补录同版本维护项：本次未提升 `pyproject.toml` 版本号，仍归入 v1.8.2。

### Added

- **多文件附件 UI** — 选多个文件后 file bar 显示"N 个文件"+ 展开箭头，点击向上弹出文件列表面板
- **逐文件删除** — 列表每行最右侧红色半透明底红色文字"删除"按钮，支持单独移除
- **一键清空恢复** — file bar 右侧 `×` 按钮，hover 变红，一键清除所有文件
- **Web 页面动效补齐** — HOME、Chat、日志页、用户管理页和会话管理面板补充入场/退场动画；Chat 顶栏、状态栏、输入框和快捷按钮按方向错峰进入，页面跳转前延迟退场。
- **本机账户选择入口** — `GET /auth/local-users` + `POST /auth/local-login` 支持从本机 JSON 用户列表选择账户并回传前端 token。

### Fixed

- **文件列表面板高度自适应** — 基于 `scrollHeight` 实测渲染高度，不再硬编码估算；低于上限自适应无缝，超过上限才滚动
- **WSL/Windows 本机登录误判** — 本机登录不再只认 loopback，新增 WSL host gateway 识别，修复 `172.19.80.1` 访问 `/auth/local-users`、`/auth/local-login` 返回 403 的问题，同时继续拒绝非本机 LAN IP。
- **状态栏 token 计数口径** — 状态栏只统计实际写入 LLM 请求上下文的内容；终端输出、工具标记等不会发送给 LLM 的记录不再污染计数。
- **上下文压缩重复触发** — 自动/手动压缩增加重复锚点保护，避免同一段上下文被连续压缩。

### Changed

- **文件存储架构** — server 不再正则解析文件前缀，`sendFile` 参数与消息文本分离传递
- **文件附件结构化** — 文件作为独立 `file` sub-step 存入 assistant content，`stripFilePrefix` 统一流式渲染和重载恢复
- **用户数据存储路径** — 用户账户改为运行态 JSON：`~/.tinda/agent/user/users.json`；旧 `~/.tinda/agent/Data/User/users.json` 仅作为迁移/兼容来源。
- **DeepSeek token 计数器接入说明** — token 估算优先加载 `~/.tinda/agent/tokenizer/` 下的 DeepSeek tokenizer，缺失时降级为启发式估算。

### Removed

- **废弃文件与旧兼容代码清理** — 移除未引用的临时测试脚本、旧 `records` 兼容接口、旧 `/tools` 兼容端点、空壳模块和手工调试产物。

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
