# TindaAgent

当前版本：`1.6.3`

TindaAgent 是一个以 Web 对话为主入口的本地 Agent 系统，核心目标是把“多轮对话、工具调用、权限控制、记录持久化、可审计日志”落在同一套可维护架构中。

本文档是项目规范与架构说明，面向开发、维护和二次集成。

## 1. 项目目标与边界

### 1.1 目标

- 提供可运行的本地 Web 对话系统（FastAPI + 前端单页）。
- 支持 LLM 工具调用闭环：模型决策 -> 工具执行 -> 结果回注。
- 支持用户与权限位管理，保证工具调用具备最小权限约束。
- 支持会话记录持久化（`txt` + `md` 双格式）与导入。
- 支持错误日志落盘，便于问题追溯。

### 1.2 非目标

- 当前版本不是多租户 SaaS，不提供远程 IAM。
- 当前版本不提供数据库级事务系统（以文件存储为主）。
- 当前版本不承诺跨节点分布式一致性。

## 2. 技术栈

- 语言：Python 3.9+
- Web 框架：FastAPI
- ASGI Server：Uvicorn
- LLM SDK：OpenAI 兼容接口（通过 `openai` 包）
- 配置加载：python-dotenv
- 前端：原生 HTML/CSS/JavaScript
- 存储：本地文件系统（JSON/TXT/MD）

## 3. 总体架构

### 3.1 分层结构

- `TindaAgent/Web`
  - Web API 入口、页面路由、流式输出、会话装配、记录读写编排。
- `TindaAgent/Process/AI`
  - Agent 对话状态机、LLM 客户端、工具调用循环控制。
- `TindaAgent/Tool`
  - 工具注册、权限校验、工具调度、内置工具集合。
- `TindaAgent/User`
  - 用户注册表、当前用户上下文、权限与 token 基础数据。
- `TindaAgent/Process/Architecture`
  - 权限位定义与任务权限映射。
- `TindaAgent/Web/records_store.py`
  - 会话记录格式、持久化、导入、分页检索。
- `TindaAgent/log/error_logger.py`
  - 错误日志统一写入（`error.log`）。

### 3.2 运行时核心对象

- `LLMClient`：负责与模型 API 通信，支持工具 schema 与工具回路。
- `Agent`：维护会话历史（含 system/fewshot 基座），对外提供普通与流式对话。
- `ChatRecordStore`：会话记录双文件持久化与恢复。
- `UserManager`：用户实体与注册表持久化。

## 4. 请求链路与数据流

### 4.1 普通对话链路（`POST /chat`）

1. Web 层接收消息，规范化 `session_id`。
2. 根据 `session_id` 获取或创建 `Agent`。
3. 若命中命令（如 `/tool`、`/tools`、`/reset`），直接命令路径返回。
4. 否则拼接用户元数据块（name/uid/perm/time）后交给 `Agent.chat_with_meta()`。
5. `Agent` 调用 `LLMClient.chat_with_tools()`：
   - 先请求模型；
   - 若模型返回工具调用，则执行工具并写入 `tool` 消息；
   - 再次请求模型直至得到最终文本或达到上限。
6. 返回 `reply + tool_trace + tool_steps`。
7. 调用 `_save_session_record()` 持久化聊天记录。

### 4.2 流式对话链路（`GET /chat/stream`）

- Web 层使用 SSE 输出事件：`delta/reset/done/error`。
- 事件消费结束后再写回最终会话记录，确保最后 assistant 消息不丢失。

### 4.3 工具调用链路

模型只暴露两个 agent 工具入口：

- `list_available_tools`
- `call_backend_tool`

`call_backend_tool` 内部流程：

1. 参数归一化（`tool_name/args/kwargs`）。
2. `run_tool()` 校验工具存在与权限位。
3. 执行工具，捕获 stdout 与返回值。
4. 输出统一 JSON 结果。

## 5. 权限模型

### 5.1 位掩码定义

在 `Process/Architecture/perm.py` 中定义：

- `PUBLIC_READ`, `PUBLIC_WRITE`, `PUBLIC_EXECUTE`
- `TOOL_READ`, `TOOL_WRITE`, `TOOL_EXECUTE`
- `SYSTEM_READ`, `SYSTEM_WRITE`, `SYSTEM_EXECUTE`

组合角色：

- `USER_VISITOR = PUBLIC_ALL`
- `USER_BASE = PUBLIC_ALL | TOOL_ALL`
- `USER_ADMIN = PUBLIC_ALL | TOOL_ALL | SYSTEM_ALL`
- `LLM_BASE = PUBLIC_ALL`

### 5.2 工具权限校验

`Tool.run_tool(tool_name, user_perm, ...)` 使用按位与校验：

- `(user_perm & required_perm) == required_perm` 才允许执行。

## 6. 数据与持久化规范

### 6.1 用户存储

- 文件：`TindaAgent/Data/User/users.json`
- 字段：`next_uid`, `users[]`
- 用户字段：`uid`, `name`, `perm`, `token`

### 6.2 聊天记录存储

- 根目录：`TindaAgent/Data/ChatRecords`
- 双文件落盘：
  - 结构化文本：`*.txt`（含 `[TINDA_RECORD]` 和 `[TINDA_MSG]`）
  - 可读文档：`*.md`
- 记录包含：
  - `role`（user/assistant）
  - `entry_type`（chat/notice/tool_marker/terminal）
  - `terminal_kind`（cmd/out/sep）
  - `ts`
  - `content`（txt 中用 base64 编码）

### 6.3 会话记录策略

- 同一 session 持续写入同一 record_id（默认按日期+时间戳创建）。
- `save_session_entries()` 支持保留非 chat 轨迹。
- `append_session_entries()` 用于增量附加终端/系统类条目。

## 7. 日志与错误处理

### 7.1 统一错误日志

- 模块：`TindaAgent/log/error_logger.py`
- 文件：`TindaAgent/Data/Log/error.log`
- 特性：
  - 线程锁串行写入
  - 记录时间、上下文、异常类型、元数据
  - 保留 traceback

### 7.2 当前接入点

已接入 Web 与 Tool 的关键异常出口，包括：

- 会话恢复失败
- 记录保存失败
- `/chat` 执行异常
- 流式 chat 异常
- session events 异常
- records import 异常
- 工具执行 validation/运行异常

## 8. API 概览

主要接口如下：

- `GET /`：主页
- `GET /app`：聊天页
- `POST /chat`：非流式对话
- `GET /chat/stream`：SSE 流式对话
- `POST /reset`：重置会话上下文
- `POST /tools`：返回当前权限可见工具
- `POST /session/events`：追加会话事件（notice/tool_marker/terminal）
- `GET /records`：分页查询记录
- `GET /records/session`：读取某会话最新记录
- `POST /records/import`：导入指定 record
- `GET /user/profile`：当前用户信息
- `GET /model`：查询当前模型与可选模型
- `POST /model`：切换模型

## 9. 内置工具清单（后端）

内置工具由 `Tool/tool.py` 注册，当前包含：

- `echo`
- `get_tinda_profile`
- `get_current_time`
- `summarize_text`
- `extract_keywords`
- `classify_intent`
- `read_profile_snippet`
- `read_memories`
- `save_memory`
- `delete_memory`
- `admin_noop`

说明：模型侧实际通过 `call_backend_tool` 代理调用以上工具。

## 10. 配置规范

### 10.1 环境变量

以 `TindaAgent/.env.example` 为模板，复制为 `.env`：

- `DEEPSEEK_API_KEY`
- `DEEPSEEK_BASE_URL`
- `DEEPSEEK_MODEL`

安全约束：

- `.env` 不得提交到 Git。
- `.env.example` 只能保留占位符，禁止写入真实密钥。

### 10.2 模型选择

当前支持：

- `deepseek-chat`
- `deepseek-v4-pro`
- `deepseek-reasoner`
- `deepseek-v4-flash`

## 11. 启动与开发

### 11.1 安装

```bash
git clone <your-repo-url>
cd TindaAgent_publish/release/source
pip install -e .
```

### 11.2 本地启动

```bash
python run_web.py
```

默认监听：`http://127.0.0.1:8000`

### 11.3 页面入口

- 首页：`/`
- 应用页：`/app`

## 12. 发布与版本规范

### 12.1 版本一致性要求

每次发布必须同步更新：

1. `pyproject.toml` 版本号
2. `README.md` 当前版本
3. Web 版本展示（`home.html` 与 `chat.html`）
4. `Process/AI/agent.py` 中 `_VERSION` 兜底值
5. `docs/CHANGELOG.md`
6. Git commit 与 GitHub push

### 12.2 推荐发布流程

```bash
git pull
# 修改版本与文档
git add -A
git commit -m "release: bump version to vX.Y.Z"
git push origin main
```

## 13. 安全基线

- 禁止将真实 API key、token、私钥写入仓库。
- 新发现密钥泄露必须立即轮换（生成新 key，撤销旧 key）。
- 保持 `.env`、本地备份、会话日志与第三方工具配置文件权限最小化。
- 对外公开仓库前，执行一次全文敏感信息扫描。

## 14. 目录结构（当前）

```text
release/source/
├── pyproject.toml
├── run_web.py
├── README.md
└── TindaAgent/
    ├── Web/
    │   ├── server.py
    │   ├── records_store.py
    │   ├── home.html
    │   └── chat.html
    ├── Process/
    │   ├── AI/
    │   │   ├── agent.py
    │   │   └── client.py
    │   └── Architecture/
    │       └── perm.py
    ├── Tool/
    │   └── tool.py
    ├── User/
    │   ├── userdata.py
    │   └── userstatus.py
    ├── log/
    │   └── error_logger.py
    ├── Data/
    │   ├── ChatRecords/
    │   ├── User/
    │   └── Log/
    └── docs/
        └── CHANGELOG.md
```

## 15. 后续建议

- 增加自动化测试（权限校验、记录读写、工具调用循环、SSE 事件序）。
- 为日志系统增加按时间分片与自动归档。
- 引入更严格的 secret pre-commit 检查。
- 为权限系统增加后端中间件级统一校验上下文。

