# TindaAgent

当前版本：`1.7.1`

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

运行时目录由子系统 `TindaAgent/Process/Architecture/paths.py` 统一决策：

- 默认基于系统环境变量 `HOME`/`USER`（Windows 兼容 `USERNAME`），相对生成 `$HOME/.tinda/agent`
- 不使用盘符或挂载路径硬编码推断

可通过环境变量 `TINDA_HOME` 显式覆盖，例如：

```bash
export TINDA_HOME=/mnt/e/.tinda/agent
```

```bash
cd /mnt/e/Python/release/source
cp .env.example TindaAgent/.env
# 编辑 TindaAgent/.env，填入 DEEPSEEK_API_KEY
conda run -n base python run_web.py
```

等价启动命令（你常用的方式）：

```bash
cd /mnt/e/Python/release/source
/mnt/e/AnacondaAnaconda3/python.exe run_web.py
```

默认访问：

- `http://127.0.0.1:8000/`
- 聊天页：`/app`
- 日志页：`/logs`

版本切换说明：

1. 在版本面板执行“切换”后，会更新 `current.json` 的“已选版本”。
2. 重启服务后，`run_web.py` 会按 `current.json.app_path` 自动从目标版本目录启动。
3. 首页会同时显示“运行版本”和“已选版本”，避免误判为“切换失败”。

本地发版包约定（强制）：

1. 每次代码版本更新后，立即为该版本创建同名快照包（目录名必须与 `pyproject.toml` 版本一致）。
2. 后端会校验快照版本与源码版本一致，不一致直接拒绝，避免“目录名和内容版本错位”。
3. 可通过接口快速执行：`POST /system/version/snapshot/current`（管理员权限）。

## 版本策略

项目版本以 `pyproject.toml` 为准；前端显示通过后端 `/system/version` 读取，避免被残留 `egg-info` 元数据污染。

版本切换与签名规则：

1. 版本源以 GitHub Releases 为准。
2. 每个发布版本必须提供 `manifest.json + manifest.sig`（Ed25519）。
3. 本地仅信任公钥验签，验签通过版本才允许安装/切换。
4. 多版本目录位于 `~/.tinda/agent/versions/<version>`，活动版本通过 `~/.tinda/agent/current.json` 指针切换。
5. 外部数据采用共享目录并在切换时自动迁移，失败自动回滚。

## 变更记录

完整历史见：

- `TindaAgent/docs/CHANGELOG.md`
