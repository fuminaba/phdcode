# phdcode

phdcode is a literature RAG tool for research students. It keeps the papers that you read, finds new papers each day, and lets an AI agent answer questions from them. Each claim in an answer cites the title, the page and an exact quote.

phdcode has no chat of its own. An **MCP server** gives its tools to any MCP client, for example Claude Code. The agent does the conversation; phdcode does the storage, the search and the parsing.

Documentation: <https://phdcode.fuminaba.com>

## What phdcode does

- **Stores papers** from arXiv, PubMed and OpenAlex, and the PDFs that you add. It parses each PDF into chunks with pages, sections, tables and figures.
- **Searches** the papers and the passages with embeddings in Postgres (pgvector).
- **Links** each mention of a figure or a table to its caption, and each citation to the reference list.
- **Runs saved searches** each day. A model judges each new hit against your library and writes a digest.
- **Shows a web page** (the interface) with the papers, the figures, the digests and a daily study question.

## Requirements

| Item | Why |
| --- | --- |
| Linux or macOS, Python 3.12 or later | phdcode itself |
| Docker | Postgres with pgvector |
| [Ollama](https://ollama.com) | The embeddings. They run on the CPU. |
| An OpenAI-compatible model endpoint | The summaries, the triage of hits and the study questions. Ollama, llama-server or a hosted API all work. |
| An MCP client | The chat, for example [Claude Code](https://claude.com/claude-code) |
| An NVIDIA GPU (optional) | A faster parse of the PDFs and a local summary model. Without a GPU, phdcode parses in text mode on the CPU. |

## Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/fuminaba/phdcode.git
   cd phdcode
   ```

2. Make a Python environment. This example uses conda:

   ```bash
   conda create -n phdcode python=3.12
   conda activate phdcode
   ```

3. Install PyTorch. On Linux with an NVIDIA GPU, use the CUDA build:

   ```bash
   pip install torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cu130
   ```

   On macOS, or on a machine without a GPU, use the CPU build:

   ```bash
   pip install torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cpu
   ```

4. Install phdcode. This also installs the commands `phdcode` and `phdcode-mcp`:

   ```bash
   pip install -e .
   ```

## Setup

### 1. Write the `.env` file

phdcode reads its settings from `.env` at the root of the repository. Git ignores this file. Make it with these values, and change each `<...>`:

```bash
# Postgres (docker-compose.yml reads these)
POSTGRES_USER=phdcode
POSTGRES_PASSWORD=<a long random password>
POSTGRES_DB=phdcode
POSTGRES_PORT=5432
DATABASE_URL=postgresql://phdcode:<the same password>@localhost:5432/phdcode

# Models: a model name with no route goes to this Ollama server
DEFAULT_SERVER_IP=http://localhost:11434
EMBEDDING_MODEL=nomic-embed-text
SUMMARY_MODEL=<an Ollama model, for example one that you pulled in step 3>

# Sources
PUBMED_EMAIL=<your email address; NCBI asks for it in each request>
```

[Configuration](https://phdcode.fuminaba.com/configuration/) lists all the settings: hosted models through `LLM_ROUTING`, API keys for OpenAlex and PubMed, chunk sizes, and the backup.

### 2. Start Postgres

```bash
docker compose up -d
```

phdcode makes the tables and the pgvector extension when it first connects.

### 3. Pull the models

```bash
ollama pull nomic-embed-text
ollama pull <the model of SUMMARY_MODEL>
```

The summaries are optional. phdcode stores and indexes each paper before any model step, so a paper is searchable when the summary model is not available. `phdcode summarize` makes the missing summaries later.

### 4. Connect your MCP client

The MCP client starts one `phdcode-mcp` process for each session. Find the full path of the command:

```bash
which phdcode-mcp
```

For Claude Code, register the server once. With `-s user`, phdcode is available in each folder:

```bash
claude mcp add -s user phdcode -- <the full path of phdcode-mcp>
```

For another MCP client, add a stdio server with the same command.

### 5. Add papers and ask

Ask the agent in plain words, for example:

- "Find recent papers on stain normalization in PubMed and add the best three."
- "Import my reference list from `~/refs.bib`."
- "What do my papers say about stain normalization? Quote the passages."

You can also use the `phdcode` command without an agent. Run `phdcode --help` for the list of commands. [Using phdcode](https://phdcode.fuminaba.com/using/) has the full workflow.

## Optional: the daily sweep

The sweep runs your saved searches, judges the new hits and writes a digest. Ask the agent to add a saved search, then run:

```bash
phdcode sweep
```

To run it each day at 06:00, use the systemd user units in `llm_engine/systemd/`. They read their paths from `.env`, so you do not edit the unit files.

1. Add the path of your Python environment to `.env`:

   ```bash
   PHDCODE_ENV=<the output of: echo $CONDA_PREFIX>
   ```

2. Link `.env` where the units read it:

   ```bash
   mkdir -p ~/.config/phdcode && ln -s "$PWD/.env" ~/.config/phdcode/env
   ```

3. Install the units:

   ```bash
   systemctl --user link "$PWD/llm_engine/systemd/phdcode-sweep.service"
   systemctl --user enable --now "$PWD/llm_engine/systemd/phdcode-sweep.timer"
   ```

`phdcode-llm.service` starts a local `llama-server` for the sweep. It needs three more values; see [Configuration](https://phdcode.fuminaba.com/configuration/).

## Optional: the interface

The interface is a web page on port 8620. Start it:

```bash
uvicorn llm_engine.dashboard:app --port 8620
```

Then open <http://127.0.0.1:8620>.

To keep it running, install its unit after steps 1 and 2 of the sweep:

```bash
systemctl --user link "$PWD/llm_engine/systemd/phdcode-dashboard.service"
systemctl --user enable --now phdcode-dashboard.service
```

**Warning: the interface has no login.** Anyone who can reach the port can read and change your library. It listens on `127.0.0.1` unless you set `UVICORN_HOST` in `.env`. Never set `UVICORN_HOST` to `0.0.0.0` or to a public address. Use a private network such as Tailscale (see below).

## Remote access with Tailscale

Use this section when phdcode runs on a server and you want to use it from a laptop.

[Tailscale](https://tailscale.com) makes a private network (a *tailnet*) between your own devices. Each device gets an address in the range `100.64.0.0/10`. Only the devices that are logged in to your tailnet can reach these addresses; the internet cannot. So the interface is safe on a Tailscale address, although it has no login.

1. Install Tailscale on the server and on the laptop, and log in to the same account on both. See <https://tailscale.com/download>.
2. On the server, get its Tailscale address:

   ```bash
   tailscale ip -4
   ```

3. On the server, add these lines to `.env`. The interface then listens on that address, and the links to figures in the answers use it:

   ```bash
   UVICORN_HOST=<the Tailscale address>
   DASHBOARD_URL=http://<the Tailscale address>:8620
   ```

4. Start the interface again. With the unit, run `systemctl --user restart phdcode-dashboard.service`. Without the unit, run `uvicorn llm_engine.dashboard:app --host <the Tailscale address> --port 8620`, because a shell does not read `.env`.
5. Start your MCP client again, so that `phdcode-mcp` reads the new `.env`.
6. On the laptop, open `http://<the Tailscale address>:8620`.

## Documentation

- [How it works](https://phdcode.fuminaba.com/how-it-works/): the parts and the data flow
- [Using phdcode](https://phdcode.fuminaba.com/using/): add papers, ask questions, back up
- [Configuration](https://phdcode.fuminaba.com/configuration/): every setting in `.env`
- [Decisions](https://phdcode.fuminaba.com/adr/): why each part is as it is

To build the documentation site on your machine:

```bash
pip install -e ".[docs]"
mkdocs serve
```
