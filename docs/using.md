# Using phdcode

You can ask an agent to do each task on this page. Claude Code and dcode both call the MCP tools, and both can run the `phdcode` command through their shell tool, after your approval. The commands are here for work without an agent, and so that you can check what the agent does.

Activate the environment of phdcode before you run a command:

```bash
conda activate alira
```

## Connect a harness

phdcode starts as one `phdcode-mcp` process for each harness session, over stdio.

**Claude Code.** Register the server once. With `-s user`, Claude Code can use phdcode from each folder; without it, only from the folder where you ran the command.

```bash
claude mcp add -s user phdcode -- <env>/bin/phdcode-mcp
```

**dcode.** Add the server to `~/.deepagents/.mcp.json`:

```json
{"mcpServers": {"phdcode": {"command": "<env>/bin/phdcode-mcp"}}}
```

`<env>` is the folder of the conda environment, for example `~/miniconda3/envs/alira`. Headless dcode (`dcode -n`) can run only the tools that do not write, because it has no approval prompt. Use the dcode TUI to add papers.

## Ask a question

Ask in plain words. The server instructions tell the agent to search the papers, then the passages, and to cite each claim with the title, the page and an exact quote. For example:

- "What do my papers say about stain normalization for prostate biopsies? Quote the passages."
- "Which of my papers report the AUC of a Gleason grading model? Show the table."
- "Summarize the methods of paper 48."

When a passage mentions a figure or a table, the agent can read the chunk with its caption (`get_chunks`), and it gives the link of the figure image on the interface.

If a claim rests only on an abstract, the agent says so: the paper has no full text.

## Add papers

### From a reference list

Export your Zotero library as BibTeX, then import it:

```bash
phdcode import ~/Downloads/library.bib
```

Each entry with a DOI, a PubMed ID or an arXiv ID becomes an abstract-only paper. An entry with none of them (a report, a web page, a patent) is skipped and listed. An entry whose DOI is already stored is skipped. The open-access PDF of each paper goes into the PDF queue.

If you have a copy of Zotero's `storage` folder on the server, give it. Its PDFs go into the queue first:

```bash
phdcode import ~/Downloads/library.bib --zotero-storage ~/zotero-storage
```

### PDFs that you downloaded

1. Copy the PDFs to the server, to any folder. Keep them there until step 3 has copied them.
2. Put each PDF into the queue of its paper:

    ```bash
    phdcode queue ~/Downloads/*.pdf
    ```

    phdcode finds the paper of each PDF by the file, the DOI, the arXiv ID or the title, else it makes a new paper ([the order](how-it-works.md#3-adding-papers)). Each line of the output names the paper and how phdcode found it. Check the titles.
3. Parse the queue:

    ```bash
    phdcode ingest
    ```

4. Make the summaries. This step starts llama-server, which holds the GPU. Start no training during it.

    ```bash
    phdcode summarize
    ```

If phdcode finds the wrong paper, give the paper yourself:

```bash
phdcode queue --paper 14 ~/Downloads/capra-s.pdf
```

### A PDF that failed to download

Many publishers refuse a download by a script. phdcode stores the reason, and the queue goes on. The interface lists these papers under the filter "PDF failed". Download each PDF in a browser, then follow [PDFs that you downloaded](#pdfs-that-you-downloaded).

### One paper from a chat

- "Save arxiv:2401.12345" calls `save_paper`: an abstract-only paper.
- "Ingest arxiv:2401.12345" or "Ingest the PDF ~/Downloads/x.pdf" calls `ingest_pdf`: it finds or makes the paper, then parses, chunks, embeds and summarizes the PDF in one call. This takes from 1 minute to several minutes. When dcode runs on the local model, the model holds the GPU, so the parse runs on the CPU. For many PDFs, `phdcode queue` is faster.

## Saved searches and the daily sweep

A saved search is a standing query at one source, with an interest statement and a threshold. Ask the agent to add one, for example: "Add a saved search on PubMed for 'prostate biopsy deep learning'. Interest: Gleason grading or cancer detection on prostate biopsies with deep learning." The agent calls `add_saved_search`.

The sweep runs each saved search and asks the summary model, for each new hit, whether the PhD student with your library would want to read it. Your library is the papers that you added and the hits that you marked as relevant. The sweep saves the hits at or above the threshold (0.3 by default), and writes the digest of the day to `logs/digests/`, ordered by the interest statement. The timer runs the PDF queue first, then the sweep, the summaries and the backup. Run the sweep by hand:

```bash
phdcode sweep
```

To run it each day at 06:00, install the timer as a user unit:

```bash
systemctl --user link <repo>/llm_engine/systemd/phdcode-sweep.service
systemctl --user enable --now <repo>/llm_engine/systemd/phdcode-sweep.timer
```

## The interface

A web page on the server shows the papers, the PDF queue, the failed PDFs, the chunks with their sections and links, the figures and tables, the digests, the saved searches and the triaged hits. It changes two things: the importance of a paper (three buttons on the paper page, ADR-0019), and the daily questions on the page Questions (ADR-0020): a click makes a question from your library, mostly from the papers you marked crucial; you answer in a paragraph and a model grades it, with a bonus point for each correct citation. "Skip: new question" replaces a question that you do not want. Two lists choose the question model and the grader from `LLM_ROUTING`. Three more lists choose the axis set (field competencies or exam areas), the competency and the question type (explain, compare, critique, design, apply); "Weakest first" and "Least practised" steer you to what you have practised least (ADR-0021). After a grade, the page shows the model answer and the key points of a strong answer. The page also shows your points, your streak, a calendar of the answers of each day, a radar of the selected axis set, and your answers by question type. A local model starts llama-server, so it waits while the GPU is cleared. Its address is `DASHBOARD_URL` ([Configuration](configuration.md)). It runs as the user unit `llm_engine/systemd/phdcode-dashboard.service`, and it reads `.env` at start: restart it after a change to `.env`.

## Backup

```bash
phdcode backup
```

The backup dumps the phdcode tables to `logs/backup/` (the last 7 dumps stay) and copies `logs/papers/` and the dumps one way to `BACKUP_REMOTE`, an rclone remote path. The copy is one-way: a file that you add at the remote is deleted at the next backup. The sweep runs the backup after each sweep.

## Training on the same GPU

Before a training run, clear the GPU. This stops llama-server, and no phdcode GPU user starts until the restore:

```bash
phdcode gpu clear
```

After the training run:

```bash
phdcode gpu restore
```

While the GPU is cleared, phdcode still works: the parses run on the CPU, and the summaries stay pending.

## After a change to the chunker

Index the parsed text of the full-text papers again, with no new parse and no model step:

```bash
phdcode rechunk
```
