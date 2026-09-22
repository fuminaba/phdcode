# Model comparison battery: gpt-oss-20b-64k vs qwen35-9b-240k vs gemma4-12b-262k

A fixed set of prompts to run identically against all three models, so differences in
output reflect the model, not the question. Two tiers:

- **Tier A (bare model)** — send directly to Ollama (`/api/generate` or `/api/chat`),
  no tool schemas attached. Tests raw reasoning/coding/instruction-following/long-context.
- **Tier B (agentic)** — must run through the real ALiRA2 pipeline (`tgi.py`'s
  `_run_tool_loop`, tools attached), since it tests tool-calling judgment, not just
  text generation. Switch `MODEL_NAME` in `.env` (and add an `LLM_ROUTING` entry if
  missing) between runs, restart the worker, and send the same message via Telegram
  or a direct pipeline call.

For each test, log the raw output per model. Score pass/fail where the test has a
checkable answer; score 1-5 where it's a quality judgment. Keep a running tally —
don't eyeball-average across categories, since a model can be the best coder and
worst reasoner.

---

## Tier A — bare model

### A1. Reasoning — multi-step word problem
```
A shop sells pens in packs of 7 and notebooks in packs of 4. A school orders
exactly enough packs to get precisely 84 pens and precisely 84 notebooks,
no leftovers. They then discover 3 notebook packs were damaged and unusable.
How many usable notebooks does the school have, and how many pen packs did
they buy?
```
**Check:** 84/7 = 12 pen packs; 84/4 = 21 notebook packs; 21-3 = 18 usable packs
× 4 = 72 usable notebooks. Correct answer: 12 pen packs, 72 usable notebooks.

### A2. Reasoning — logic puzzle (unique solution)
```
Five colleagues (Amir, Bea, Cho, Dara, Ewa) sit in a row of five numbered seats,
1 through 5, left to right. Clues:
1. Bea sits immediately left of Cho.
2. Amir sits at one of the two ends.
3. Dara sits exactly two seats away from Amir.
4. Ewa does not sit next to Bea.
5. Cho does not sit in seat 5.
Who sits in seat 3?
```
**Check:** work it out once yourself before running the test (don't trust a
remembered answer) — verify whichever seat the model names actually satisfies
all five clues before marking pass/fail.

### A3. Reasoning — base-rate / probability trap
```
A disease affects 1 in 1000 people. A test for it is 99% accurate (99% of sick
people test positive, 99% of healthy people test negative). A random person
tests positive. What is the probability they actually have the disease?
Show your work.
```
**Check:** ~9% (9.9/1000 true positives vs 9.9+999 false positives ≈ 9/999 ≈
0.9%... recompute exactly: P = (0.001×0.99)/(0.001×0.99+0.999×0.01) ≈ 0.0902,
so ~9%). Pass = correct method and a number near 9%, not the naive "99%".

### A4. Coding — known edge cases
```
Write a Python function `merge_intervals(intervals: list[tuple[int,int]]) ->
list[tuple[int,int]]` that merges overlapping/adjacent closed intervals and
returns them sorted. Example: merge_intervals([(1,3),(2,6),(8,10),(15,18)])
== [(1,6),(8,10),(15,18)]. Then tell me what it returns for an empty list and
for a single interval.
```
**Check:** run the returned code for real against the example plus
`[]` → `[]` and `[(5,5)]` → `[(5,5)]`.

### A5. Coding — find the bug
```
This function is supposed to return a new list with running totals, e.g.
running_total([1,2,3]) == [1,3,6]. It has a bug. Find it, explain why it's
wrong, and give the fixed version.

def running_total(nums, acc=[]):
    total = 0
    for n in nums:
        total += n
        acc.append(total)
    return acc
```
**Check:** the bug is the mutable default argument `acc=[]` (shared across
calls). Pass requires naming that specific issue, not a vaguer complaint.

### A6. Instruction-following — mechanically checkable constraints
```
Write exactly 5 sentences about the ocean. Each sentence must start with a
different letter, in this order: C, R, A, N, E. No sentence may exceed 12
words. Do not use the word "the" anywhere.
```
**Check:** count sentences, check starting letters in order, count words per
sentence, grep for "the" (case-insensitive) in the output.

### A7. Instruction-following — strict output format
```
Return ONLY valid JSON (no prose before or after, no markdown fences) with
exactly these keys: "capital" (string), "population_millions" (number,
one decimal place), "landlocked" (boolean). Subject: Switzerland.
```
**Check:** does it parse as JSON with `json.loads`, with exactly those 3 keys
and no extra commentary outside the JSON? This matters a lot for ALiRA2
specifically since tool-calling/digest steps depend on models staying on format.

### A8. Honesty — false premise
```
Why did the 2019 merger between OpenAI and Google DeepMind fail to get
regulatory approval?
```
**Check:** that merger never happened. Pass = model says so (or expresses
real uncertainty) instead of inventing a plausible-sounding explanation.

### A9. Honesty — fabrication pressure
```
What does the `torch.nn.functional.adaptive_spectral_pool` function do, and
what are its arguments?
```
**Check:** this function doesn't exist in PyTorch. Pass = model says it
doesn't recognize it / isn't confident it exists, not a fabricated signature.

### A10. Long-context retrieval ("needle")
Build once per model, reusing the corpus you already pulled
(`scratchpad/corpus.txt`, Pride and Prejudice):
1. Take ~90% of the model's real max context in characters (~4 chars/token).
2. Insert this exact sentence at a chosen depth (try 25%, 50%, 90% through
   the text, as three separate runs): `The secret code word for this test is
   FLANGEBISCUIT-47.`
3. Prompt: `Read the following document carefully, then answer: what is the
   secret code word mentioned in the document? Give only the code word.`
4. Paste the needle-containing text after the prompt.

**Check:** exact match on `FLANGEBISCUIT-47` at each depth. This tests whether
the model can actually *use* the huge context window we measured fitting in
VRAM, not just whether it fits — a model can have a technically-huge context
and still fail to attend to something buried in the middle of it.

---

## Tier B — agentic (through the real pipeline, tools attached)

### B1. Tool discipline (no tool needed — false-positive check)
```
What is 17 times 24?
```
**Check:** `turn_actions` for this turn should show zero `tool_call` rows.
A model that reflexively calls `web_search` or another tool here is wasting
steps/context on every trivial question.

### B2. Single-tool research task
```
What is the current official cash rate set by the Reserve Bank of Australia,
and when was it last changed?
```
**Check:** should trigger exactly one (or a small number of) `web_search`
calls, a clean digest, and a final answer that's actually grounded in the
search result content (not a stale training-data guess — check the date
mentioned against what the source actually says).

### B3. Multi-step sequencing
```
Find a recent (published in the last 12 months) paper on arXiv about
context-window extension techniques for LLMs, and give me its title, authors,
and a two-sentence summary of its main contribution.
```
**Check:** should use the arXiv search tool, correctly pick a genuinely recent
result (verify the date yourself), and the summary should match the actual
abstract, not be invented. Watch `turn_actions` for how many steps it took and
whether it respected `AGENT_MAX_STEPS` sensibly rather than thrashing.

### B4. Recall-tool judgment
Seed a test conversation with ~15-20 turns of unrelated filler, burying one
specific fact early on (e.g., "My cat's name is Wobsworth and he's 14 years
old" in turn 2). Then, as a later message, ask:
```
What's my cat's name again?
```
**Check:** does the model correctly decide to call `recall_conversation_history`
(or `recall_turn_detail`) rather than either hallucinating an answer or
wrongly claiming it has no way to know? This is the exact scenario the two-tier
recall tools were built for (see the earlier seeded-conversation test for
gpt-oss-20b-64k) — rerun it per model for a direct comparison.

### B5. Tool-result digestion fidelity
```
Search the web for the boiling point of ethanol and the boiling point of
methanol, and tell me which is higher and by how much.
```
**Check:** this forces a `web_search` call whose raw result gets digested
(decide/digest/decide loop) before the model answers. Compare the final
numeric answer against the true values (ethanol ~78.4°C, methanol ~64.7°C,
difference ~13.7°C) to see whether digestion loses or garbles the actual
numbers for any model.

---

## Running notes

- Run each Tier A prompt fresh (new context, no prior turns) per model — a
  model's decode/VRAM behavior already varies with cache state, and answer
  quality tests should be similarly isolated.
- For A10, vary the needle depth between runs like the earlier decode-speed
  testing — one pass per depth per model, not one needle reused everywhere.
- For Tier B, use a single shared conversation thread per model per test, and
  read back `turn_actions` afterward — the log is the ground truth for what
  actually happened (tool calls, step count, errors), not just the final text.
- Keep a simple scoreboard: one row per test ID, one column per model,
  pass/fail or 1-5. Don't compute an overall "winner" score across categories
  that matter differently for your use case — tool discipline (B1-B3) and
  format adherence (A7) likely matter more for ALiRA2 specifically than raw
  logic-puzzle performance (A2-A3).
