from TindaAgent.User import userdata


def add_user(name: str, perm: int, usertoken: str | None = None) -> userdata.UserManager:
    """
    用处： 添加用户到系统
    """
    user = userdata.UserManager(name, perm, usertoken)
    return user


def get_user_from_name(name: str) -> userdata.UserManager | None:
    """
    用处： 根据用户名获取用户对象
    """
    return userdata.get_user_from_name(name)


def get_user_from_uid(uid: str) -> userdata.UserManager | None:
    """
    用处： 根据用户ID获取用户对象
    """
    return userdata.get_user_from_uid(uid)


class UserStatus:
    """
    用处： 用户状态管理类
    """

    def __init__(self) -> None:
        self.current_user: userdata.UserManager | None = None

    def set_current_user(self, user: userdata.UserManager) -> None:
        self.current_user = user

    def get_current_user(self) -> userdata.UserManager | None:
        return self.current_user

    def get_perm(self) -> int:
        if self.current_user is None:
            raise RuntimeError("当前没有已登录用户")
        return self.current_user.perm


user = UserStatus()
user.set_current_user(userdata.ensure_default_user("Tinda"))
