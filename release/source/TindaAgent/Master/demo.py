from TindaAgent.Process.AI.agent import Agent
from TindaAgent.Output import output


def main() -> None:
    """
    用处： 最小可用 demo —— 启动一个多轮对话 REPL
    命令：
        /exit 退出
        /reset 清空对话历史
    """
    output.info("TindaAgent Demo —— 输入 /exit 退出，/reset 清空历史")
    agent = Agent("demo-bot")

    while True:
        try:
            user_input = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input == "/exit":
            break
        if user_input == "/reset":
            agent.reset_history()
            output.info("已清空对话历史")
            continue

        try:
            reply = agent.chat(user_input)
            print(f"\n🤖 > {reply}")
        except Exception as e:
            output.error("CHAT", False, f"调用失败: {e}")

    output.info("再见")


if __name__ == "__main__":
    main()
