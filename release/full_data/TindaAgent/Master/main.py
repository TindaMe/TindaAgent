from TindaAgent.User import userdata, userstatus
from TindaAgent.Process.Architecture import perm
from TindaAgent.Tool import encrypt, tool

TOKEN_LENGTH = 64

def main() -> None:
    userstatus.user = userdata.UserManager("test", perm.USER_ADMIN, encrypt.tokens_str_generator(TOKEN_LENGTH))
    print(userstatus.user.get_perm())
    print(userstatus.user.get_name())
    print(userstatus.user.get_token())
    user_perm = userstatus.user.get_perm()
    if user_perm & perm.USER_ADMIN == perm.USER_ADMIN:
        print("用户是管理员")
    else:
        print("用户不是管理员")
    print(user_perm)

if __name__ == "__main__":
    main()