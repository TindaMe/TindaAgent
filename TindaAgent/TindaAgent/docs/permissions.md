# TindaAgent 权限系统说明（产品/运营版）

本文说明当前项目的权限模型、每个权限位的适用范围，以及在模块/接口/工具三级中的实际生效位置。

## 1. 权限模型总览

权限采用位掩码（bitmask）设计，账号权限是多个权限位的组合。

### 1.1 权限位定义

| 权限名 | 位值 | 十进制 | 含义 |
|---|---:|---:|---|
| `PUBLIC_READ` | `1 << 0` | 1 | 公开数据读权限 |
| `PUBLIC_WRITE` | `1 << 1` | 2 | 公开数据写权限 |
| `PUBLIC_EXECUTE` | `1 << 2` | 4 | 公开能力执行权限 |
| `TOOL_READ` | `1 << 3` | 8 | 账户调用工具时的读权限 |
| `TOOL_WRITE` | `1 << 4` | 16 | 账户调用工具时的写权限 |
| `TOOL_EXECUTE` | `1 << 5` | 32 | 账户调用工具时的执行权限 |
| `SYSTEM_READ` | `1 << 6` | 64 | 账户访问系统资源时的读权限 |
| `SYSTEM_WRITE` | `1 << 7` | 128 | 账户访问系统资源时的写权限 |
| `SYSTEM_EXECUTE` | `1 << 8` | 256 | 账户访问系统能力时的执行权限 |

### 1.2 组合权限与角色

| 名称 | 十进制值 | 说明 |
|---|---:|---|
| `PUBLIC_ALL` | 7 | 公开域全权限（读/写/执行） |
| `TOOL_ALL` | 56 | 工具域全权限（读/写/执行） |
| `SYSTEM_ALL` | 448 | 系统域全权限（读/写/执行） |
| `USER_VISITOR` | 7 | 访客角色，仅公开域 |
| `USER_BASE` | 63 | 基础角色，公开域 + 工具域 |
| `USER_ADMIN` | 511 | 管理员，公开域 + 工具域 + 系统域 |
| `LLM_BASE` | 7 | 模型默认权限，等同公开域 |

## 2. 每个权限的适用范围

以下按“模块 / 接口 / 工具”说明每个权限位适用边界。

### 2.1 `PUBLIC_READ`
- 定义：读取公开数据。
- 模块：Tool 注册与执行、Web 工具查询。
- 接口：`POST /tools`（仅返回当前权限可见工具），工具命令入口（`/chat`、`/chat/stream`、`/sessions/{id}/tool-jobs`）中的读取型工具调用。
- 工具：`get_tinda_profile`、`get_current_time`、`summarize_text`、`extract_keywords`、`read_profile_snippet`、`read_memories`。
- 不适用：写入或删除数据。

### 2.2 `PUBLIC_WRITE`
- 定义：写入/删除公开数据。
- 模块：Tool 执行层（数据写入工具）。
- 接口：同工具命令入口，实际由工具权限校验控制。
- 工具：`save_memory`、`delete_memory`。
- 风险：误授会导致记忆数据被修改或删除。

### 2.3 `PUBLIC_EXECUTE`
- 定义：执行公开能力。
- 模块：Tool 执行层。
- 接口：同工具命令入口。
- 工具：`echo`。
- 说明：当前公开执行型工具较少，但执行位已纳入统一模型。

### 2.4 `TOOL_READ`
- 定义：账户在“工具域”内的读能力。
- 模块：权限架构层（位定义与角色组合）。
- 接口：当前 Web/Tool 流程尚未将现有工具挂到 `TOOL_READ` 位。
- 工具：当前无工具直接要求该位。
- 适用建议：未来若引入“仅工具域可读数据”（非公开数据），应绑定此位。

### 2.5 `TOOL_WRITE`
- 定义：账户在“工具域”内的写能力。
- 模块：权限架构层。
- 接口：当前未绑定。
- 工具：当前无工具直接要求该位。
- 适用建议：未来若引入工具侧配置写入、知识库写入，可绑定此位。

### 2.6 `TOOL_EXECUTE`
- 定义：账户在“工具域”内的执行能力。
- 模块：权限架构层。
- 接口：当前未绑定。
- 工具：当前无工具直接要求该位。
- 适用建议：未来若引入高风险工具执行（例如外部系统动作），应绑定此位。

### 2.7 `SYSTEM_READ`
- 定义：账户访问系统资源时的读能力。
- 模块：权限架构层。
- 接口：当前未在 Web 业务接口上单独启用该位门禁。
- 工具：当前无工具直接要求该位。
- 适用建议：系统级日志、配置、审计读操作应绑定此位。

### 2.8 `SYSTEM_WRITE`
- 定义：账户访问系统资源时的写能力。
- 模块：权限架构层。
- 接口：当前未单独启用该位门禁。
- 工具：当前无工具直接要求该位。
- 适用建议：系统配置变更、资源写入应绑定此位。

### 2.9 `SYSTEM_EXECUTE`
- 定义：账户访问系统能力时的执行权限。
- 模块：权限架构层。
- 接口：当前未单独启用该位门禁。
- 工具：当前无工具直接要求该位。
- 适用建议：系统任务调度、管理动作应绑定此位。

## 3. 三级适用范围矩阵（模块 / 接口 / 工具）

### 3.1 模块级
- `Process/Architecture/perm.py`：权限位、组合权限、角色权限的唯一来源。
- `Tool/tool.py`：工具注册声明“需要的权限位”，并在 `run_tool()` 做强校验。
- `Web/server.py`：读取当前用户权限并传入工具运行时。
- `Web/tool_runtime.py`：每个会话独立线程执行工具，按提交时 `user_perm` 调用工具。
- `Process/AI/agent.py`：Agent 默认权限为 `LLM_BASE`（公开域）。

### 3.2 接口级
- `GET /user/profile`：返回 `perm` 与 `perm_label`，用于前端展示当前账号权限。
- `POST /chat`：当消息以 `/` 开头，作为工具命令提交，使用当前用户 `perm`。
- `GET /chat/stream`：流式场景下同样提交工具命令并按用户 `perm` 执行。
- `POST /sessions/{session_id}/tool-jobs`：直接创建工具任务，按用户 `perm` 执行。
- `POST /tools`（兼容接口）：返回当前用户可见工具列表（按权限过滤）。

### 3.3 工具级（当前已注册工具）

| 工具 | 需要权限 | 适用范围 |
|---|---|---|
| `echo` | `PUBLIC_EXECUTE` | 公开执行 |
| `get_tinda_profile` | `PUBLIC_READ` | 公开读取 |
| `get_current_time` | `PUBLIC_READ` | 公开读取 |
| `summarize_text` | `PUBLIC_READ` | 公开读取 |
| `extract_keywords` | `PUBLIC_READ` | 公开读取 |
| `read_profile_snippet` | `PUBLIC_READ` | 公开读取 |
| `read_memories` | `PUBLIC_READ` | 公开读取 |
| `save_memory` | `PUBLIC_WRITE` | 公开写入 |
| `delete_memory` | `PUBLIC_WRITE` | 公开写入 |

## 4. 运行时校验规则

工具执行统一使用以下规则校验：

`(user_perm & required_perm) == required_perm`

含义：用户权限必须完整包含工具要求的权限位，否则拒绝执行并返回“权限不足”。

示例：
- 用户权限 `7 (PUBLIC_ALL)` 调用 `save_memory (PUBLIC_WRITE=2)`：允许。
- 用户权限 `1 (仅 PUBLIC_READ)` 调用 `delete_memory (PUBLIC_WRITE=2)`：拒绝。

## 5. 运营使用建议

- 默认对话与模型侧账号使用 `LLM_BASE`（公开域）更稳妥。
- 给人工运营账号分配权限时优先最小授权：能读不写，能写不执行高风险动作。
- 出现“调用 XXX 权限不足”时，先检查：
  1. 当前账号 `perm` 值
  2. 目标工具所需权限
  3. 是否满足位掩码包含关系
- `TOOL_*` 与 `SYSTEM_*` 已在模型中定义为独立域权限；当业务引入非公开工具能力或系统级操作时，建议优先使用这两组位进行隔离授权。

## 6. 源码依据（维护时对照）

- `Process/Architecture/perm.py`
- `Tool/tool.py`
- `Web/server.py`
- `Web/tool_runtime.py`
- `Process/AI/agent.py`
