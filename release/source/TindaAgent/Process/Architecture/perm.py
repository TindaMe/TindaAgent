# 权限设计

# 权限位
PUBLIC_READ = 1 << 0
PUBLIC_WRITE = 1 << 1
PUBLIC_EXECUTE = 1 << 2
TOOL_READ = 1 << 3
TOOL_WRITE = 1 << 4
TOOL_EXECUTE = 1 << 5
SYSTEM_READ = 1 << 6
SYSTEM_WRITE = 1 << 7
SYSTEM_EXECUTE = 1 << 8

# 权限组合
PUBLIC_ALL = PUBLIC_READ | PUBLIC_WRITE | PUBLIC_EXECUTE
TOOL_ALL = TOOL_READ | TOOL_WRITE | TOOL_EXECUTE
SYSTEM_ALL = SYSTEM_READ | SYSTEM_WRITE | SYSTEM_EXECUTE

# 身份权限组合
USER_VISITOR = PUBLIC_ALL
USER_BASE = USER_VISITOR | TOOL_ALL
USER_ADMIN = USER_BASE | SYSTEM_ALL
LLM_BASE = PUBLIC_ALL

class PermManager:
    task_dict: dict[str, int] = {}
    @classmethod
    def add_task(cls, task: str, perm: int) -> None:
        """
        用处： 添加任务到权限管理器中

        参数：
            task: str // 任务的名称
            perm: int // 任务的权限

        返回：
            None
        """
        if task in cls.task_dict:
            raise ValueError(f"任务{task}已存在")
        cls.task_dict[task] = perm
    @classmethod
    def remove_task(cls, task: str) -> None:
        """
        用处： 从权限管理器中移除任务

        参数：
            task: str // 任务的名称

        返回：
            None
        """
        try:
            del cls.task_dict[task]
        except KeyError as e:
            raise KeyError(f"任务{task}不存在") from e