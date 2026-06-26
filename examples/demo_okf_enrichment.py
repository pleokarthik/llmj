from core.config import load_project_env
load_project_env()
from core.journal_store import Store
from core.vector_embedder import Embedder
from core.llm_client import LLMClient
from handshake.okf_enrichment import OKFEnricher, enrich_run
from handshake.session_runner import start_run, end_run

store = Store("journal.db")
embedder = Embedder("google")
llm = LLMClient(store)

# 1. Run with one LLM call
root_id = start_run("test-chat-7", "user_message", store)
llm.call(
    messages=[{"role": "user", "content": "Explain WAL mode in SQLite."}],
    provider="groq",
    model="llama-3.3-70b-versatile",
    chat_id="test-chat-7",
    root_id=root_id,
)
end_run(root_id, "run_completed", store)

# 2. Enrich
paths = enrich_run(root_id, store, embedder, llm)
print("bundles written:", paths)

# 3. Confirm bundle is self-contained
import json
bundle = json.load(open(paths[0]))
print("bundle keys:", list(bundle.keys()))
print("provenance:", bundle.get("provenance"))

# 4. Confirm enrichment event journaled
import sqlite3
c = sqlite3.connect("journal.db")
rows = c.execute("SELECT DISTINCT origin FROM events").fetchall()
print("origins in journal:", [r[0] for r in rows])
