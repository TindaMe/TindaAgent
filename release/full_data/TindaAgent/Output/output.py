# 输出模块

# 全局变量
INFO = "INFO"
WARNING = "WARNING"
ERROR = "ERROR"

# 函数定义
def output(msg: str) -> None:
    """
    用处： 输出信息

    参数：
    msg: 要输出的信息

    目前为标准终端输出
    """
    print(msg)
def info(msg: str) -> None:
    """
    用处： 输出信息
    
    参数：
    msg: 要输出的信息
    """
    output(f"[{INFO}] {msg}")
def warning(msg: str) -> None:
    """
    用处： 输出警告信息
    
    参数：
    msg: 警告信息
    """
    output(f"[{WARNING}] {msg}")
def error(error_type: str,should_pause: bool, msg: str) -> None:
    """
    用处： 输出错误信息
    
    参数：
    error_type: 错误类型
    should_pause: 是否暂停程序
    msg: 错误信息
    """
    output(f"[{ERROR}] [{error_type}] [STATUS:{should_pause}] {msg}")

