from core.config import load_project_env
load_project_env()
from core.journal_store import Store
from core.llm_client import LLMClient
from core.tool_runner import ToolRunner
from handshake.session_runner import start_run, end_run, resume_run, get_run_status, reconcile_crashed_runs

store = Store("journal.db")
llm = LLMClient(store)

# 1. Start a run
root_id = start_run("test-chat", "user_message", store)
print(f"Started run: {root_id}")

# 2. Execute a tool
runner = ToolRunner(store)
result = runner.run(
    tool_name="add",
    tool_fn=lambda a, b: a + b,
    args={"a": 1, "b": 2},
    root_id=root_id,
    chat_id="test-chat",
)
print(f"Tool result: {result}")

# 3. End the run cleanly
end_run(root_id, "run_completed", store)
status = get_run_status(root_id, store)
print(f"Run status: {status}")

# 4. Simulate a crashed run
crashed_id = start_run("test-chat", "background_task", store)
print(f"Started (will crash): {crashed_id}")
crashed = reconcile_crashed_runs(store, crashed_threshold_seconds=0)
print(f"Reconciled crashed runs: {crashed}")

# 5. Resume a run
root_id2 = start_run("test-chat", "resumable_task", store)
resume_run(root_id2, store)
status2 = get_run_status(root_id2, store)
print(f"Resumed run status: {status2}")

# 6. Confirm event types in journal
import sqlite3
c = sqlite3.connect("journal.db")
rows = c.execute("SELECT DISTINCT event_type FROM events").fetchall()
print("Event types in journal:", [r[0] for r in rows])
