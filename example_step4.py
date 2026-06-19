from core.config import load_project_env
load_project_env()
from core.store import Store
from core.embedder import Embedder
from core.llm_client import LLMClient
from core.handshake import switch_model

store = Store("journal.db")
embedder = Embedder("google")
llm = LLMClient(store)

messages = [
    {"role": "user", "content": "What is the capital of France?"},
    {"role": "assistant", "content": "The capital of France is Paris."},
    {"role": "user", "content": "What is its population?"},
    {"role": "assistant", "content": "Paris has a population of about 2.1 million in the city proper."},
]

response = switch_model(
    chat_id="test-chat",
    messages=messages,
    user_first_query="What else should I know about Paris?",
    store=store,
    embedder=embedder,
    llm_client=llm,
)

print("B's first response:", response)

rows = list(store.query({"origin": "system:summarizer"}))
print(f"Summarizer events in journal: {len(rows)}")
print(f"Summarizer event_id: {rows[-1].event_id}")
