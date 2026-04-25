# TindaAgent

当前版本：`1.6.6`

TindaAgent 是一个本地化 Web Agent 系统，聚焦于以下能力：

1. 对话与工具调用闭环（LLM -> Tool -> 回注）
2. 基于位掩码的权限体系
3. 会话记录持久化（聊天/终端/提示同链路）
4. 审计日志与错误日志
5. 可切换模型与会话管理

## 目录与职责

`TindaAgent/Web`

- FastAPI 接口与页面路由
- 聊天流式输出与工具轨迹回传
- 会话管理、记录加载与导入

`TindaAgent/Process/AI`

- `Agent`：会话历史、系统提示、工具循环
- `client`：LLM 请求、工具调用回路、DSML 兼容
- Provider 抽象（为多模型适配预留）

`TindaAgent/Tool`

- 工具注册与调度
- 参数归一化、权限校验、标准结果结构

`TindaAgent/Permission`

- 权限引擎
- 工具最小权限策略
- 权限不足结构化返回

`TindaAgent/Process/Observability`

- 全局审计日志引擎
- 统一事件 ID
- 总日志 / 子系统日志 / 错误日志

## 存储与运行时路径

默认运行时目录：

- `~/.tinda/agent`

可通过环境变量覆盖：

- `TINDA_HOME=/your/path`

常用子目录：

- `~/.tinda/agent/Data`：会话、系统数据、用户数据
- `~/.tinda/agent/log`：`total.jsonl`、子系统日志、`error.log`

## 启动（AnacondaAnaconda3）

```bash
conda run -n base python /mnt/e/Python/TindaAgent/run_web.py
```

默认访问：

- `http://127.0.0.1:8000/`
- 聊天页：`/app`
- 日志页：`/logs`

## 版本策略

项目版本以 `pyproject.toml` 为准；前端显示通过后端 `/system/version` 读取，避免被残留 `egg-info` 元数据污染。

## 变更记录

完整历史见：

- `TindaAgent/docs/CHANGELOG.md`
