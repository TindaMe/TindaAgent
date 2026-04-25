# TindaAgent

一个带有权限控制的智能体（Agent）骨架系统。

> 当前版本：`1.6.2`

## 特性

- 🔐 **位掩码权限系统** — 按 `PUBLIC / TOOL / SYSTEM` 三档 × `R / W / X` 组合，灵活授权
- 👤 **用户与身份管理** — `UserManager` 维护用户名、权限、自动生成 64 位十六进制 token
- 🛠 **工具注册 + 权限调度** — 通过 `@tool` 装饰器注册函数，`run_tool` 按权限校验后调用
- 🤖 **Agent 抽象** — 智能体继承用户权限，基于 `check_perm` 决定能否执行任务
- 🔑 **加密工具** — SHA-256、随机 token、百万次加盐哈希
- 📝 **统一输出** — `info / warning / error` 三档日志

## 安装

```bash
git clone <your-repo-url>
cd TindaAgent
pip install -e .
```

依赖（见 `TindaAgent/requirements.txt`）：

```text
openai
python-dotenv
```

## 目录结构

```
TindaAgent/
├── pyproject.toml
└── TindaAgent/
    ├── Master/              # 程序入口（main.py）
    ├── Process/
    │   ├── AI/              # Agent 核心类
    │   └── Architecture/    # 权限系统、文件写入
    ├── User/                # 用户数据与登录态
    ├── Tool/                # 工具装饰器、加密工具
    ├── Output/              # 日志输出
    ├── Data/                # 预留：数据目录
    ├── Database/            # 预留：持久化
    ├── Input/               # 预留：输入处理
    └── log/                 # 运行日志
```

## 快速开始

### 创建用户并检查权限

```python
from TindaAgent.User import userdata, userstatus
from TindaAgent.Process.Architecture import perm
from TindaAgent.Tool import encrypt

TOKEN_LENGTH = 64

user = userdata.UserManager(
    "alice",
    perm.USER_ADMIN,
    encrypt.tokens_str_generator(TOKEN_LENGTH),
)
userstatus.user.set_current_user(user)

print(user.get_name(), user.get_perm())

if user.get_perm() & perm.USER_ADMIN == perm.USER_ADMIN:
    print("管理员用户")
```

### 注册并调用一个工具

```python
from TindaAgent.Tool import tool
from TindaAgent.Process.Architecture import perm

@tool(perm.PUBLIC_EXECUTE, "打印问候语", must=False)
def greet(name: str) -> None:
    print(f"hello, {name}")

# 按权限调度（权限不足会被拒绝）
tool.run_tool("greet", perm.PUBLIC_ALL, "world")

# 列出当前权限能调用的所有工具
print(tool.list_tools(perm.PUBLIC_ALL))
```

### 创建一个 Agent

```python
from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Process.Architecture import perm

bot = Agent("assistant")  # 默认使用 LLM_BASE 权限
perm.PermManager.add_task("echo", perm.PUBLIC_EXECUTE)

if bot.check_perm("echo"):
    print("可以执行 echo")
```

## 权限体系

| 权限位 | 十进制 | 含义 |
|--------|--------|------|
| `PUBLIC_READ`    | 1    | 公共读 |
| `PUBLIC_WRITE`   | 2    | 公共写 |
| `PUBLIC_EXECUTE` | 4    | 公共执行 |
| `TOOL_READ`      | 8    | 工具读 |
| `TOOL_WRITE`     | 16   | 工具写 |
| `TOOL_EXECUTE`   | 32   | 工具执行 |
| `SYSTEM_READ`    | 64   | 系统读 |
| `SYSTEM_WRITE`   | 128  | 系统写 |
| `SYSTEM_EXECUTE` | 256  | 系统执行 |

组合身份：

| 身份 | 权限范围 |
|------|----------|
| `USER_VISITOR` | `PUBLIC_ALL` |
| `USER_BASE`    | `PUBLIC_ALL \| TOOL_ALL` |
| `USER_ADMIN`   | 全部权限 |
| `LLM_BASE`     | `PUBLIC_ALL` |

## 路线图

- [ ] 接入 LLM（OpenAI 兼容接口），让 `Agent` 具备真实推理能力
- [ ] 用户数据持久化（SQLite / JSON）
- [ ] 实现 `get_user_from_name` / `get_user_from_uid` 查询
- [ ] 单元测试（pytest）
- [ ] 输入模块（`Input/`）与数据目录（`Data/`）具体实现

## 许可证

暂未指定。
