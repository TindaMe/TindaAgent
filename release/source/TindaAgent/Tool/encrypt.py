import hashlib
import secrets

# 常量
ONE_MILLION: int = 1000000

def bytes_to_sha256(bytes_content: bytes) -> bytes:
    """
    用处： 计算bytes数据的sha256值

    参数：
        bytes_content: bytes // bytes类型的实例
    """
    sha256_bytes_content = hashlib.sha256(bytes_content)
    bytes_result = sha256_bytes_content.digest()
    return bytes_result

def get_sha256_bytes(content: str) -> bytes:
    """
    用处： 计算str数据的sha256值

    参数：
        content: str // 需要计算的sha256数据
    """
    bytes_content = content.encode("utf-8")
    bytes_result = bytes_to_sha256(bytes_content)
    return bytes_result

def get_sha256_str(content: str) -> str:
    """
    用处： 计算文本数据的sha256值

    参数：
        content: str // 输入一个需要计算sha256值的str对象
    """
    bytes_result = get_sha256_bytes(content)
    str_result = bytes_result.hex()
    return str_result

def tokens_bytes_generator(length: int) -> bytes:
    """
    用处： 生成随机指定长度tokens

    参数：
        length: int // 生成token的字符串长度
    """
    if length <= 0 or length % 2 != 0:
        raise ValueError("length 必须是正偶数")
    temporary_length = length // 2
    bytes_raw_tokens = secrets.token_bytes(temporary_length)
    return bytes_raw_tokens

def tokens_str_generator(length: int) -> str:
    """
    用处： 生成随机指定长度tokens

    参数：
        length: int // 生成token的字符串长度
    """
    bytes_raw_tokens = tokens_bytes_generator(length)
    str_raw_tokens = bytes_raw_tokens.hex()
    return str_raw_tokens


def key_generator() -> str:
    """
    用处： 随机生成str类型的64长度密钥

    参数：
    """
    key = tokens_str_generator(64)
    return key

def safe_bytes_to_sha256_bytes(content: bytes, salt: bytes) -> bytes:
    """
    用处： 把bytes类型转换为重sha256值的bytes类型实例

    参数：
        content: bytes // bytes类型的实例
        salt: bytes // bytes类型的盐值
    """
    temporary_result = content
    for _ in range(0, ONE_MILLION):
        temporary_result = bytes_to_sha256(temporary_result + salt)
    bytes_result = temporary_result
    return bytes_result

def safe_str_to_sha256_bytes(content: str, salt: str) -> bytes:
    """
    用处： 把str类型转换为重哈希sha256值的bytes类型实例

    参数：
        content: str // 需要计算的sha256数据
        salt: str // str类型的盐值
    """
    bytes_content = content.encode("utf-8")
    bytes_salt = salt.encode("utf-8")
    result = safe_bytes_to_sha256_bytes(bytes_content, bytes_salt)
    return result

def safe_sha256_str(content: str, salt: str) -> str:
    """
    用处： 重复计算哈希避免爆破

    参数：
        content: str // str类型的数据
        salt: str // str类型随机生成盐
    """
    bytes_result = safe_str_to_sha256_bytes(content, salt)
    str_result = bytes_result.hex()
    return str_result

def demo() -> None:
    """
    用处： 演示各函数的使用方法

    参数：
    """
    content: str = "nihao"
    result = get_sha256_str(content)
    print(result)
    raw_tokens = tokens_str_generator(64)
    print(raw_tokens)
    key = key_generator()
    print(key)
    safe_sha256_result = safe_sha256_str(content, raw_tokens)
    print(safe_sha256_result)

if __name__ == "__main__":
    demo()
