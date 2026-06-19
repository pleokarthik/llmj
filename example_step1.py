import os

from core import LLMClient, Store

DEFAULT_MODELS = {
    "openai": "gpt-3.5-turbo",
    "google": "gemini-2.0-flash",
    "groq": "llama-3.3-70b-versatile",
}


def main() -> None:
    provider = os.environ.get("LLMJ_PROVIDER", "openai")
    model = os.environ.get("LLMJ_MODEL", DEFAULT_MODELS.get(provider, "gpt-3.5-turbo"))
    store = Store("journal.db")
    llm = LLMClient(store)
    try:
        response = llm.call(
            chat_id="test-chat",
            messages=[
                {"role": "user", "content": "Write a short greeting in one sentence."}
            ],
            provider=provider,
            model=model,
        )
        print("LLM response:")
        print(response)
    except Exception as exc:
        print(f"LLM call failed: {exc}")

    print("Stored events:")
    for event in store.query({"chat_id": "test-chat"}):
        print(event)


if __name__ == "__main__":
    main()
