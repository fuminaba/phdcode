# Using phdcode

You can ask an agent to do each task on this page. Claude Code and dcode both call the MCP tools, and both can run the `phdcode` command through their shell tool, after your approval. The commands are here for work without an agent, and so that you can check what the agent does.

Activate the environment of phdcode before you run a command:

```bash
conda activate phdcode
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

`<env>` is the folder of the conda environment, for example `~/miniconda3/envs/phdcode`. Headless dcode (`dcode -n`) can run only the tools that do not write, because it has no approval prompt. Use the dcode TUI to add papers.

## Ask a question

Ask in plain words. The server instructions tell the agent to search the papers, then the passages, and to cite each claim with the title, the page and an exact quote. For example:

- "What do my papers say about stain normalization for prostate biopsies? Quote the passages."
- "Which of my papers report the AUC of a Gleason grading model? Show the table."
- "Summarize the methods of paper 48."

When a passage mentions a figure or a table, the agent can read the chunk with its caption (`get_chunks`), and it gives the link of the figure image on the interface.

If a claim rests only on an abstract, the agent says so: the paper has no full text.

### On the page Ask

The interface has the same agent on its page Ask (ADR-0030). Type the question, select the model, and click Ask:

- **bonsai2-27b** runs dcode with Bonsai 2 on llama-server: a few minutes, and it waits while the GPU is cleared.
- **A Claude model** runs Claude Code (`claude -p`) on your Claude login: about 15 s.

While the agent works, the page shows each search. The answer cites each paper as `[P<id>]`, a link to its page. A tag shows how many quotes of the answer the page found word for word in the cited papers. The quotes that it did not find are in the closed section under the answer.

The agent on this page has three tools only: `search_papers`, `search_chunks` and `get_chunks`. It cannot change the library or search outside it. One question runs at a time. The page keeps only the last answer, until the interface restarts.

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

In the interface, click **Upload PDFs** on the page Papers (several PDFs), or **Upload the PDF** on the page of a paper (one PDF for that paper). Each PDF goes into the queue, and the interface starts `phdcode ingest`, then `phdcode summarize`, in the background (ADR-0031). On the server, the same steps are:

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

The sweep runs each saved search and asks the summary model, for each new hit, whether the PhD student with your library would want to read it. Your library is the papers that you added and the hits that you marked as relevant. The sweep saves the hits at or above the threshold (0.3 by default), and writes the digest of the day to `logs/digests/`, ordered by the interest statement. The page Digests shows each day as a card that opens and closes: for each saved search (by a short name), its relevant papers with their authors, date and triage probability, linked to their pages; the searches with no relevant hit share one line. The timer runs the PDF queue first, then the sweep, the summaries and the backup. Run the sweep by hand:

```bash
phdcode sweep
```

To run it each day at 06:00, install the timer as a user unit:

```bash
systemctl --user link <repo>/llm_engine/systemd/phdcode-sweep.service
systemctl --user enable --now <repo>/llm_engine/systemd/phdcode-sweep.timer
```

## The interface

A web page on the server, on the Tailscale address only. It has five tabs (ADR-0031):

- **Overview:** your activity (the day streak, the activities of each week and of each day: questions answered, writing tasks, papers marked as read), the paper of the day (ADR-0024), and the library in numbers. What runs and the configuration are in closed sections.
- **Papers**, with five sub-tabs:
    - **Papers:** the table of papers, with a filter and sortable columns, and **Upload PDFs**. The page of a paper shows its chunks, figures, tables and triage, and sets its importance (ADR-0019), its notes and its read mark (ADR-0026).
    - **Interests:** the saved searches. Open one to change its interest, its query or its threshold, or to cancel it; **New saved search** adds one. A changed query runs from the last sweep on.
    - **Labels:** each judged hit, to label as relevant or not (ADR-0027).
    - **Digests:** the digest of each sweep, as day cards.
    - **Bin:** the deleted papers, to restore (ADR-0026).
- **Ask:** an agent answers a question from the library ([On the page Ask](#on-the-page-ask)).
- **Skill development**, with two sub-tabs:
    - **Questions:** daily questions from your library, graded by a model, with a score history and a tutor chat (ADR-0020 to ADR-0023).
    - **Writing:** a writing task with scores for six criteria, and a revision (ADR-0025).
- **How it works:** the diagrams of the tool.

A local model starts llama-server, so it waits while the GPU is cleared. The address of the interface is `DASHBOARD_URL` ([Configuration](configuration.md)). It runs as the user unit `llm_engine/systemd/phdcode-dashboard.service`, and it reads `.env` at start: restart it after a change to `.env`.

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
