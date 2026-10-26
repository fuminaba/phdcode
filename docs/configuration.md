# Configuration

phdcode reads its settings from `.env` at the root of the repository. A variable that is set in the environment wins over `.env`: a test sets `DATABASE_URL` to the test database this way.

## Storage

| Variable | Default | What it sets |
| --- | --- | --- |
| `DATABASE_URL` | none (required) | The Postgres database with pgvector, for example `postgresql://user:password@localhost:5432/phdcode`. |
| `POSTGRES_USER`, `POSTGRES_DB` | none | The user and the database of `pg_dump` in `phdcode backup`. |
| `POSTGRES_CONTAINER` | `alira2-postgres-1` | The Docker container where `pg_dump` runs. |
| `LOG_DIR` | `logs` | The folder of the paper folders (`<LOG_DIR>/papers/<paper_id>/`) and the dumps (`<LOG_DIR>/backup/`), relative to the repository. A test sets another folder, so that its paper IDs do not share folders with the live papers. |
| `DIGEST_DIR` | `logs/digests` | The folder of the daily digests. |
| `BACKUP_REMOTE` | none | The rclone remote path of the backup, for example `gdrive:phdcode-papers`. With none, `phdcode backup` makes only the dump. |

## Models

| Variable | Default | What it sets |
| --- | --- | --- |
| `LLM_ROUTING` | none | The model routes: for each model name, its OpenAI-compatible endpoint and key. Set `'local': false` for a hosted model, so that the GPU rules do not count it as a GPU tenant. `'provider': 'claude-code'` calls Claude through the Claude Code command (`claude -p`) on the account that is logged in there, with no endpoint and no API key, for example `'claude-haiku-5-5': {'provider': 'claude-code', 'local': false}`. See `llm_engine/routes.py`. |
| `DEFAULT_SERVER_IP` | none | The base URL of the Ollama server for a model name that has no route, for example `http://localhost:11434` (phdcode adds `/v1`). |
| `SUMMARY_MODEL` | `AGENT_BACKEND` | The model of the methods and results summaries, and of the metadata of a local PDF. |
| `EMBEDDING_MODEL` | `nomic-embed-text` | The Ollama embedding model. It runs on the CPU. |
| `EMBEDDING_DIM` | `768` | The dimension of the embeddings. It must match `EMBEDDING_MODEL`. A change needs new embeddings for all papers. |
| `QUESTION_MODEL` | `SUMMARY_MODEL` | The default model that writes the daily questions and their model answers (ADR-0020); the page Questions can choose another route of `LLM_ROUTING`, local or hosted. |
| `GRADE_MODEL` | `QUESTION_MODEL` | The default model that grades the answers; the page can choose another. |
| `SYSONE_DIR`, `SYSONE_PYTHON` | none | The absolute paths of the repository of the System One head and of the Python that runs it (ADR-0028). With none, `phdcode headscore` scores nothing. |
| `BRIEF_MODEL` | `claude-haiku-5-5` | The LLM that writes the briefs (ADR-0032): the interests paragraph, the triage brief and the summary of findings of each day of the digest. A route of `LLM_ROUTING`; the menu of each brief can choose another for one brief. |
| `TRIAGE_MODEL` | `SUMMARY_MODEL` | The LLM that judges each hit of the sweep: does the student with this library want it? If its server cannot answer (the GPU is cleared for training), the hits wait for the next sweep. |

## Chunks

| Variable | Default | What it sets |
| --- | --- | --- |
| `DOCUMENT_CHUNK_SIZE` | `1500` | The size of a chunk, in characters. A table or a figure with its caption can make a larger chunk. |
| `DOCUMENT_CHUNK_OVERLAP` | `200` | The overlap between two chunks, in characters. The overlap never repeats a table or a figure. |
| `DOCUMENT_MAP_CHUNK_SIZE` | `8000` | The size of a part of the text for the summaries. It must fit in the context of `SUMMARY_MODEL`. |

After a change to the chunk settings, run `phdcode rechunk`.

## Sources

| Variable | Default | What it sets |
| --- | --- | --- |
| `OPENALEX_API_KEY` | none | The OpenAlex key. With none, the daily budget is about 100 searches; a lookup by ID is free. |
| `PUBMED_EMAIL` | none | The email address that NCBI asks for in each request. |
| `PUBMED_API_KEY` | none | The NCBI key: 10 requests a second instead of 3. |

## The interface

| Variable | Default | What it sets |
| --- | --- | --- |
| `DASHBOARD_URL` | `http://localhost:8620` | The address of the interface. The MCP tools use it for the links to figures. |
| `UVICORN_HOST` | `127.0.0.1` | The address where the interface listens. The interface has no login: use a private address only, for example the Tailscale address. |

## The systemd units

The units in `llm_engine/systemd/` read `.env` through the link `~/.config/phdcode/env`, because the path of a unit file cannot come from a variable. Make the link once:

```bash
mkdir -p ~/.config/phdcode && ln -s <repo>/.env ~/.config/phdcode/env
```

| Variable | Default | What it sets |
| --- | --- | --- |
| `PHDCODE_ENV` | none (required) | The absolute path of the Python environment where phdcode is installed, for example `/home/<user>/miniconda3/envs/phdcode`. |
| `LLAMA_SERVER_BIN` | none | The absolute path of the `llama-server` program (`phdcode-llm.service`). |
| `LLAMA_MODELS_PRESET` | none | The absolute path of the models preset file of `llama-server`. |
| `LLAMA_VRAM_MIB` | none | The free VRAM, in MiB, that the local model needs. `llama-server` does not start with less. |
