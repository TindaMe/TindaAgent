import json
import pathlib

# 基础函数
def str_in_file(file_path: pathlib.Path, content: str) -> None: 
    """
    用处： 写入字符串到文件
    """
    file_path.write_text(content, encoding="utf-8")

def json_in_file(file_path: pathlib.Path, data: dict) -> None:
    """
    用处： 写入json数据到文件
    """
    file_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )

def user_json(user: dict, file_path: pathlib.Path) -> None: 
    """
    用处： 写入用户json数据的dict格式写入文件
    """
    json_in_file(file_path, user)
