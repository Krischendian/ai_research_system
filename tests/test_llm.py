"""OpenAI 连通性测试：需在项目根 .env 配置有效 OPENAI_API_KEY。

执行：
PYTHONPATH=src python tests/test_llm.py
"""
from research_automation.extractors.llm_client import chat


def main() -> None:
    reply = chat("用一句中文确认：LLM 客户端连接成功。")
    print(reply)
    assert reply, "返回内容为空"


if __name__ == "__main__":
    main()
