# ADR-0036: A weekly batch of the open-access PDFs, parsed on a GPU VM

**Status:** Accepted — 2026-11-16. Item 6 (Haiku) superseded by [ADR-0037](0037-summaries-on-a-bonsai-vm.md) on 2026-11-17.
**Supersedes:** the rejected alternative "download of open-access PDFs during the sweep" of [ADR-0015](0015-rescope-to-literature-rag-tool.md), and its daily sweep (item 6)
**Amends:** [ADR-0017](0017-gpu-main-slot.md) (the batch parses in balanced mode on a VM, not on the GPU of the server); [ADR-0031](0031-interface-tabs-uploads-and-saved-searches.md) (an upload only joins the queue)

## Context

ADR-0015 rejected the download of open-access PDFs during the sweep: "It puts
work on the GPU that nobody asked for." That premise is no longer true. Fumi
asks for it (2026-11-16): both libraries are mostly abstract-only (the PhD
library 318 of 365 papers, the second library 224 of 224), and an abstract gives the
Ask page and the agents little to quote.

Most of these papers have a free PDF. arXiv papers have a direct link. PubMed
gives a PMC id for 148 of them, and PMC's own `/pdf/` link answers with a page
that checks for bots, and Europe PMC's `?pdf=render` with HTTP 403. The PMC
open-access data set on S3 (`pmc-oa-opendata`) serves the PDF with no login.
OpenAlex knows the PDF locations of a DOI, but not the PMC id of recent
papers. Most publishers name their PDF in the tag `citation_pdf_url` of the
DOI landing page.

The parse of a backlog of hundreds of PDFs does not belong on the one GPU of
the server, which training and Bonsai share (ADR-0017). VMQ
(`~/Desktop/dev/VMQ`) runs a job on a new Hyperstack VM and deletes the VM
after it; its profile `surya` sets up Surya 2.5 on vLLM and marker 2.2.0 on an
RTX A4000 (19 pages in 28 s, about $0.025 for a test job, 2026-11-15).

## Decision

1. **`phdcode fetch`** looks at the abstract-only papers with no PDF in the
   queue and no failure, newest first, and tries their links in order: the
   stored `pdf_url` (not PubMed's PMC page); the PMC data set on S3 (the PMC
   id of the PubMed link, else the NCBI ID converter by DOI); the PDF
   locations of OpenAlex by DOI; `citation_pdf_url` of the DOI landing page.
   The first link whose first bytes are `%PDF` goes into the PDF queue
   (`pdf_source`).
2. **Open access only.** No login and no proxy of a library: a paywalled paper
   stays abstract-only until Fumi uploads its PDF.
3. **A miss comes back after 30 days.** A paper with no PDF gets `pdf_error`
   "no open-access PDF found on YYYY-MM-DD"; the fetch tries it again 30 days
   later, after the new papers (a PMC deposit can come months after the
   publication). A failure of a lookup service (OpenAlex, the ID converter,
   S3) changes nothing: the next fetch tries the paper again. Such a paper
   takes no slot of a `--limit`, the fetch stops after 5 of them in a row (a
   service is down), and the command exits with 1, so that the unit and its
   monitor show the failure. A failed link of a publisher is a miss of that
   link. An arXiv link is not checked (arXiv asks for 3 s between requests);
   the VM checks each download.
4. **`phdcode pdfs` parses the queue on a VMQ VM.** It fetches all papers,
   then submits one VMQ job (profile `surya`) with a manifest of the queue:
   the URLs, and the local uploads that wait. On the VM, `parse.sh` downloads
   each PDF and runs `marker_single --mode balanced --paginate_output`; a
   failed paper goes into `failed.tsv` and the loop goes on. phdcode waits for
   the job, then imports each result as a full-text paper with `parse_mode`
   `balanced` (`index_text`, as `phdcode rechunk`). A paper that failed on the
   VM gets `pdf_error`; a paper that the VM could not download goes back to
   the fetch as a dated miss; a paper with no result (the job stopped) goes
   back to the next fetch, an upload back into the queue. While a job is
   open, its papers are held (`pdf_source` "vmq:<job>"), so that
   `phdcode ingest` (an upload on the interface) does not parse them on the
   server. phdcode waits up to 24 h for a job in the VMQ queue (no stock, or
   the job of the other deployment), then up to `max_hours` + 1 h for the run;
   a job that is still open stays for the next run. The state of each job is in its folder,
   `logs/vmq/<database>-<time>/`, so that a job that ends during a reboot is
   imported by the next run.
5. **Weekly, Monday.** `phdcode-sweep.timer` runs at 06:00: the sweep,
   `headscore`, `pdfs`, `summarize`, `backup`. `MAX_HITS` is 200 (a saved
   search had 50 hits in 7 days). A second deployment runs `pdfs` and
   `summarize` in `phdcode-pdfs@<name>.timer` at 06:30, with `python -m` in
   the folder of its clone (the `phdcode` command imports the first clone);
   VMQ runs one job at a time, so its job waits behind the job of the sweep.
6. **The summaries use Claude Haiku** (`SUMMARY_MODEL=claude-haiku-5-5` in the
   `.env` of each clone): no GPU.
7. **The button "Parse the queue now"** on the Overview page runs `pdfs`, then
   `summarize`, at once, as the transient user unit
   `phdcode-pdfs-now-<clone folder>` in the folder of the clone of the
   interface. An upload only joins the queue: it no longer starts
   `phdcode ingest` on the server (amends ADR-0031). `phdcode pdfs` takes a
   lock in `logs/vmq/`, so one run at a time works for each library: a run of
   the button can wait hours for its job, and the Monday run must not import
   that job a second time.
8. **Paywalled papers: a download by hand.** A paper of the library (not of
   triage) with a dated miss has the state "paywalled" on the page Papers
   (`/papers?filter=paywalled`, the link of the Overview). Its row has a link
   to the OpenURL resolver of the library (`LIBRARY_RESOLVER`, UBC eLink) with
   the DOI, else the title and the venue, and an upload button. The resolver
   lists the licensed full text through the library login, also at an
   aggregator, and interlibrary loan. A DOI link through the OpenAthens prefix
   failed for old publisher URLs (2026-11-17). No login goes into a script.

## Consequences

- The GPU of the server no longer parses the queue. `phdcode ingest` stays a
  command for a parse on the server by hand.
- A press of the button with a large queue starts a large VM job: the first
  press parses the backlog.
- The VM costs money from Fumi's personal Hyperstack account: $0.15/h on an
  A4000, $0.50 on an A6000, $1.00 on an L40 (2026-11-16). VMQ stops at its
  monthly limit; a job that VMQ refuses sends its papers back to the next
  fetch.
- Haiku takes about 2.3 min for the two summaries of one paper (measured on
  2 papers): a backlog of 400 papers is about 16 h of `claude -p` calls on
  Fumi's subscription; a summary that fails stays pending for the next run.
- `SUMMARY_MODEL` is also the default model of the writing coach and the
  puzzles when the page gives none. `QUESTION_MODEL` stays `bonsai2-27b`, so
  that the questions and the paper of the day do not change.
- The digest comes once a week, and the summaries of a batch some hours after
  the sweep.
- `phdcode fetch --dry-run` prints the link of each paper and changes nothing.
- The page of a paper with a miss shows "PDF failed", with the reason in
  `pdf_error`.
- Not used: Unpaywall. It needs an email address in each request. Its extra
  hits over OpenAlex are not measured; in the dry run of 2026-11-16, OpenAlex
  gave 2 working links that the other sources did not.
- Rejected: the parse on the server GPU, 20 papers a day (the first version of
  this ADR): a backlog of 540 papers would hold the shared GPU for weeks.
- Rejected: a download of the paywalled PDFs with the library login of Fumi in
  a script, at any pace. The licences of UBC forbid systematic downloading,
  and a publisher can block the access of the whole university.
  The publishers' text and data mining programmes (Elsevier, Wiley) stay
  possible after a request to the library.
- Rejected: a marker server that runs all the time on a VM. A new VM for each
  weekly job costs only the hours of the parse.
