# llmj

Local-first cross-provider LLM journal implementation.

## Step 1 implementation

- `core/store.py`: SQLite journal with WAL mode and immutable `events` table.
- `core/llm_client.py`: OpenAI-backed `LLMClient` that journals a start and completion event.
- `core/models.py`: typed `Event` model matching DDD schema exactly.
- `core/ulid.py`: pure-Python ULID generation.
- `example_step1.py`: end-to-end demo script.

## Run

You can set project-scoped environment variables in a `.env` file at the project root:

```env
GOOGLE_API_KEY=your-key-here
LLMJ_PROVIDER=google
```

The code loads `.env` automatically when `LLMClient` is initialized.

Alternatively, set variables in the shell:

- `OPENAI_API_KEY` for OpenAI
- `GOOGLE_API_KEY` for Google Gemini
- `LLMJ_PROVIDER=openai` or `LLMJ_PROVIDER=google` to choose the provider

```bash
python example_step1.py
```
