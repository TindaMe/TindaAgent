# TindaAgent

当前版本：`1.7.10`

TindaAgent 是一个本地化 Web Agent 系统，聚焦于以下能力：

1. 对话与工具调用闭环（LLM -> Tool -> 回注）
2. 基于位掩码的权限体系
3. 会话记录持久化（聊天/终端/提示同链路）
4. 审计日志与错误日志
5. 可切换模型与会话管理
6. 模型能力检测页（连接 / 思考支持 / 图片 / 视频）

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

## 启动

```bash
cp .env.example TindaAgent/.env
# 编辑 TindaAgent/.env，填入 DEEPSEEK_API_KEY
python run_web.py
```

默认访问：

- `http://127.0.0.1:8000/`
- 聊天页：`/app`
- 设置页：`/settings`
- 日志页：`/logs`
- 模型检测页：`/model-diagnostics`

## 模型检测能力（v1.7.5）

模型检测页用于做最小片段能力验证，避免“模型看起来可用但实际能力缺失”。

入口：

1. 聊天页头部“模型检测”按钮（仅有 LLM 权限用户可见）
2. 直接访问 `/model-diagnostics`

检测项：

1. `connectivity`：连接测试（最小文本请求）
2. `reasoning`：思考支持测试（是否返回 `reasoning_content`）
3. `image`：图片测试（基于 `image_url`）
4. `video`：视频测试（基于 `video_url`）

后端接口：

- `POST /model-diagnostics/run`

请求示例：

```json
{
  "model": "deepseek-v4-flash",
  "tests": ["connectivity", "reasoning", "image", "video"],
  "image_url": "https://example.com/demo.png",
  "video_url": "https://example.com/demo.mp4"
}
```

权限规则：

1. 必须登录
2. 必须具备 LLM 执行权限（`PUBLIC_EXECUTE`，位值 `4`）
3. 无权限返回 `403`

结果规则：

1. 状态枚举：`pass` / `fail` / `unsupported` / `skipped`
2. 每项返回耗时、摘要、错误信息、结果片段
3. 检测结果仅页面展示，不写入会话消息存储

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
