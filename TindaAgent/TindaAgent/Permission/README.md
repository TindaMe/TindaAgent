# Permission Engine 与系统接入计划

本目录用于统一管理权限引擎与工具最小权限策略。

版本状态：`v1.6.6`

## 1. 目标

- 将权限判断从分散实现收敛到 Permission 引擎。
- 维护一份工具最小权限清单（`tool_min_permissions.json`）。
- 权限不足时：给 LLM 返回结构化提示，不向用户暴露权限细节。

## 2. 关键文件

- `tool_min_permissions.json`：工具到最小权限的单一来源。
- `engine.py`：权限判断、权限标签、缺失权限解释、权限不足 payload。
- `errors.py`：`PermissionDeniedError` 结构化异常。
- `__init__.py`：统一导出。

## 3. 接入规则

- Tool 注册时用策略文件校准权限（策略优先，装饰器为兼容兜底）。
- Tool 运行时统一使用 `has_perm`。
- `permission_denied` 返回结构包含：
  - `llm_message`（给模型）
  - `user_message`（给用户可展示提示）
  - `error`（前端/终端可读的错误详情）
  - `exec_id/call_id`（调用追踪ID）
  - `expose_to_user=false`
- Web/前端默认展示 `error`，不再只显示固定通用文案。

## 4. 当前最小权限映射

- `echo`: `PUBLIC_EXECUTE`
- `get_tinda_profile/get_current_time/summarize_text/extract_keywords/read_profile_snippet/read_memories`: `PUBLIC_READ`
- `save_memory/delete_memory`: `PUBLIC_WRITE`

## 5. 验收要点

- 工具权限与清单一致；不一致时有告警。
- 权限不足时，LLM 能看到缺失权限信息并继续推理。
- 用户端可看到可读错误与调用ID，便于日志检索与问题定位。

## 6. 近期修复（v1.6.6）

- 修复“权限拒绝只显示通用提示”问题，改为可返回详细错误链路。
- 修复“调用追踪ID缺失”问题，统一接入 `exec_id/call_id` 透传。
- 对齐日志命名：主错误日志为 `error.log`，兼容 `audit_error.log`。
