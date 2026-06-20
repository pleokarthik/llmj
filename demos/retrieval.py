from core.config import load_project_env
load_project_env()
from core.store import Store
from core.embedder import Embedder
from core.llm_client import LLMClient
from core.context import assemble_context

store = Store("journal.db")
embedder = Embedder("google")
llm = LLMClient(store)

# 1. Write a user turn and get a model response
response = llm.call(
    messages=[{"role": "user", "content": "What is the capital of France?"}],
    provider="groq",
    model="llama-3.3-70b-versatile",
    chat_id="test-chat-6",
)
content = response["choices"][0]["message"]["content"]
print("Response:", content)

# 2. Upsert only clean assistant responses into vectors
for e in store.query({"chat_id": "test-chat-6"}):
    if e.role == "assistant" and e.status == "ok" and e.content:
        v = embedder.embed(e.content[:500])
        store.upsert_vector(e.event_id, e.content[:500], v,
                            embedding_provider=embedder.provider_name,
                            embedding_model=embedder.model_name)

# 3. Retrieve and confirm model_claim tagging
result = assemble_context("test-chat-6", "France capital", store, embedder, llm)
print(result)

# 4. Confirm origin on assistant event
import sqlite3
c = sqlite3.connect("journal.db")
rows = c.execute("SELECT role, origin, status FROM events WHERE chat_id='test-chat-6'").fetchall()
for r in rows:
    print(r)
