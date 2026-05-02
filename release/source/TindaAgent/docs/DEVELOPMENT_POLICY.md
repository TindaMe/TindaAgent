# TindaAgent Development Policy

本文档定义代码修改、测试与发布前验证的硬约束。目标是确保新增功能可回归、可追踪、可跨环境运行。

## 1. 修改原则

1. 每个功能改动必须是可验证的最小增量，不允许把多个不相关问题混在同一提交里。
2. 不允许只改提示词或只改前端文案来掩盖后端流程问题；必须修复根因。
3. 涉及跨平台行为（Windows/WSL/Linux）时，必须同时给出三端的可观测结果。

## 2. 测试硬要求

1. 新增功能必须同步新增测试；没有测试的功能改动不允许合入。
2. Bug 修复必须新增“失败用例 -> 修复后通过”的回归测试。
3. 脚本变更（`start.*` / `stop.*` / `status.*` / `doctor.*`）至少包含：
   - 语法检查
   - 参数检查
   - 关键路径执行检查
4. 工具调用链路变更（`tool_runtime.py` / `client.py` / `server.py`）必须新增单测覆盖异常分支。

## 3. 必跑检查（提交前）

在项目根目录执行：

```bash
./doctor.sh
```

或（Windows）：

```bat
doctor.bat
```

`doctor` 默认执行以下检查：

1. Python 版本与运行时可用性。
2. 关键文件存在性。
3. 关键 Python 文件 `py_compile`。
4. Linux 脚本语法检查（`bash -n`）。
5. Windows 脚本可执行检查（从 WSL/Windows 触发）。
6. 端口追踪与监听状态一致性检查（`.tinda_ports.list` vs 实际监听）。
7. 本地 HTTP 可达性探测（`/app`、`/chat`、`/`）。
8. 临时启动探针（启动 `run_web.py` 并验证端口与 HTTP）。
9. `unittest discover` 全量测试。

## 4. 文档同步要求

1. 任意新增功能，必须在 `TindaAgent/docs/CHANGELOG.md` 增加条目。
2. 任意影响架构流程的改动，必须更新 `TindaAgent/docs/architecture.md`。
3. 新增脚本或命令参数，必须在本文件和对应脚本头注释中写明用途。

## 5. 端口与进程约束

1. `run_web.py` 是唯一端口追踪写入源，统一维护 `.tinda_ports.list`。
2. `stop.*` 只以 `.tinda_ports.list` 为默认真值，避免环境变量历史污染。
3. 需要合并环境变量时，显式开启 `TINDA_PORTS_INCLUDE_ENV=1`。

## 6. Linux 启动后网页不可达排查顺序

1. `./status.sh --show` 看监听端口是否存在。
2. `./doctor.sh --skip-tests` 看 `startup.http` 与 `http.probe`。
3. WSL 场景从 Windows 浏览器访问 `http://127.0.0.1:<port>/chat`。
4. 若端口监听正常但 HTTP 失败，优先排查防火墙/代理与本机端口占用。

## 7. 禁止事项

1. 禁止在日志、终端输出、错误信息中打印明文密钥。
2. 禁止提交未通过 doctor 的改动。
3. 禁止删除历史追踪文件而不补充迁移/兼容逻辑。
