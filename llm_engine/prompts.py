from typing import Optional


def json_schema_instruction(json_schema: str) -> str:
    return (
        "Reply with one JSON object (double quotes) that matches this JSON "
        f"Schema: {json_schema}. Do not include any extra text, only the "
        "required information."
    )


def chunk_digest_prompt(chunk_text: str) -> str:
    return (
        "Condense the following excerpt from a larger document into a short, "
        "dense digest (3-6 sentences) capturing its key topic, any methodology "
        "or procedures described, and any results, data, or findings reported. "
        "Omit anything not covered by those categories. If the excerpt is just "
        "references, boilerplate, or contains none of these, say so in one "
        "short sentence rather than padding with irrelevant detail.\n\n"
        f"Excerpt:\n{chunk_text}"
    )


def document_metadata_prompt(document_text: str) -> str:
    return (
        "Extract metadata from the following document. Copy the abstract "
        "word for word; do not summarize it. If it isn't a research "
        "article/paper, do your best with what's actually present "
        "— use an empty list for authors/keywords and an empty string for "
        "the abstract if none apply, rather than inventing them.\n\n"
        f"Document:\n{document_text}"
    )


def methods_summary_prompt(document_text: str) -> str:
    return (
        "Summarize the methodology used in the following document — study "
        "design, techniques, tools, procedures, data sources, sample sizes, "
        "etc. If the document doesn't describe a methodology, say so "
        "explicitly rather than inventing one. "
        "The document can come as digests of its sections: write about the "
        "paper itself, and do not mention the excerpts, the digests or the "
        "reference list.\n\n"
        f"Document:\n{document_text}"
    )


def results_summary_prompt(document_text: str) -> str:
    return (
        "Summarize the results/findings reported in the following document "
        "— key outcomes, measurements, statistical findings, and "
        "conclusions drawn from the data. If the document doesn't report "
        "results, say so explicitly rather than inventing them. "
        "The document can come as digests of its sections: write about the "
        "paper itself, and do not mention the excerpts, the digests or the "
        "reference list.\n\n"
        f"Document:\n{document_text}"
    )




def triage_prompt(library: str, title: str, abstract: str) -> str:
    # The wording of the test against Fumi's labels (ADR-0018); a change
    # needs the same test.
    return (
        f"A PhD student keeps this library of papers:\n{library}\n\n"
        "A new paper appeared. Give the probability (0 to 1) that this student "
        "would want to read it.\n\n"
        f"Title: {title}\n\nAbstract: {abstract or '(none)'}"
    )


def question_prompt(papers: str, focus: str, question_type: str, axis_sets: dict[str, list[str]]) -> str:
    # The daily question (ADR-0020, ADR-0021).
    lists = "\n".join(f"- {name}: {'; '.join(axes)}" for name, axes in axis_sets.items())
    return (
        "You examine a PhD student in medical image analysis with machine "
        "learning for oncology. Write one question that the student should be "
        "able to answer in one paragraph after reading these papers.\n\n"
        f"The question tests this competency: {focus}.\n"
        f"The kind of question: {question_type}.\n\n"
        "Ask about an idea, a method, a design choice, a limitation or a "
        "comparison, not about a number or a detail that only memory gives. "
        "Prefer a question that needs more than one of the papers. Use the "
        "papers that fit the competency; if none fits well, ask what the "
        "competency means for the work of these papers. Do not name the "
        "papers or their ids in the question; give in the question the facts "
        "that the student needs.\n\n"
        "Then write the model answer: one paragraph that cites the papers by "
        "their ids in brackets, for example [P12]. In key_points, list the 3 "
        "to 5 points that a strong answer must contain, each in one "
        "sentence.\n\n"
        "In tags, give one flat list of labels: from each list below, the one "
        "or two labels that the question tests, copied exactly:\n"
        f"{lists}\n\n"
        f"Papers:\n{papers}\n\n"
        "Check before you reply: the question names no paper, no author and "
        "no id such as P12; only the model answer and the key points cite ids."
    )


def review_topics_instruction(known: list[str]) -> str:
    # The concepts to brush up on (ADR-0033). known: the names of earlier
    # answers, so that a repeated gap gets the same name and is counted.
    reuse = f" Reuse a name from this list when it fits: {'; '.join(known)}." if known else ""
    return (
        " In review_topics, list 0 to 3 concepts or subconcepts that the "
        "answer shows the student should brush up on, each as a short name "
        "of 2 to 6 words, general enough to come up again (for example "
        "\"Bayes' rule with low base rates\", not a sentence about this "
        "answer). Leave it empty if the answer shows no gap." + reuse
    )


def grade_prompt(question: str, model_answer: str, key_points: list[str], papers: str, library: str,
                 answer: str, known_topics: list[str]) -> str:
    # The grade of the daily question (ADR-0020, ADR-0021).
    points = "\n".join(f"- {p}" for p in key_points) or "(none)"
    return (
        "You grade the answer of a PhD student to an exam question.\n\n"
        f"Question: {question}\n\nModel answer: {model_answer}\n\n"
        f"Key points of a strong answer:\n{points}\n\n"
        f"The papers of the question:\n{papers}\n\n"
        f"The student's library (id: title, first author, year):\n{library}\n\n"
        f"The student's answer: {answer}\n\n"
        "Give a score from 0 to 10 for correctness, depth and clarity, mostly "
        "by the key points that the answer covers; a different correct answer "
        "than the model answer is fine. List in "
        "cited_papers the id numbers (12 for P12) of the library papers that "
        "the answer cites, by title, author, year or id, where the paper "
        "supports the claim. Leave out a citation that is wrong. In feedback, "
        "write 2 to 4 sentences: what was right, what was missing or wrong."
        + review_topics_instruction(known_topics)
    )


def chat_prompt(question: str, papers: str, graded: Optional[str], conversation: str, message: str) -> str:
    # The tutor of a daily question (ADR-0023). graded: the model answer, the
    # key points, the student's answer and the feedback; None before a grade.
    rule = (
        "The student has not answered yet. Clarify the question, its terms "
        "and its background, and say which papers are useful, but do not give "
        "the answer or its key points."
        if graded is None else
        f"The student has answered, and the answer was graded:\n{graded}\n\n"
        "Discuss freely: explain the model answer, what the student's answer "
        "missed or got wrong, and what a stronger answer needs."
    )
    return (
        "You are the tutor of a PhD student in medical image analysis with "
        "machine learning for oncology. The student works on this exam "
        f"question:\n{question}\n\nPapers:\n{papers}\n\n{rule}\n\n"
        f"Conversation so far:\n{conversation or '(none)'}\n\n"
        f"Student: {message}\n\n"
        "Reply to the last message of the student in at most 200 words. Cite "
        "a paper by its id in brackets, for example [P12], when you use it."
    )



def daily_paper_prompt(paper: str) -> str:
    # The paper of the day (ADR-0024).
    return (
        "You prepare a PhD student in medical image analysis with machine "
        "learning for oncology for the thesis defence. Write a brief of this "
        "paper from the student's library.\n\n"
        f"Paper:\n{paper}\n\n"
        "In about, write one paragraph for a reader who has not read the "
        "paper: the problem, why it matters, and the approach. In summary, "
        "write one paragraph: the data, the method and the main results with "
        "their key numbers. In key_points, list the 3 to 5 points that the "
        "student must know for the defence, each in one or two sentences: the "
        "main claim, the evidence for it, its limitations, and what it means "
        "for computational pathology. In examiner_questions, list 2 or 3 "
        "questions that an examiner could ask about this paper. Use only "
        "facts from the paper; if the text gives no number, do not invent one."
    )


def writing_prompt_prompt(kind: str, paper: str) -> str:
    # The writing coach: a new prompt (ADR-0025).
    return (
        "You coach a PhD student in medical image analysis with machine "
        "learning for oncology in scientific writing. Write one writing task "
        "that takes one or two paragraphs to answer.\n\n"
        f"The kind of writing: {kind}.\n\n"
        "Use this paper as the subject. Give in the task all the facts that "
        "the student needs, so that the task tests the writing, not the "
        f"memory:\n{paper}\n\n"
        "In prompt, write the task. In audience, name the reader in a few "
        "words. In max_words, give the word limit (between 100 and 300)."
    )


def writing_review_prompt(kind: str, prompt: str, audience: str, max_words: int, criteria: dict[str, str],
                          text: str, earlier: Optional[str]) -> str:
    # The writing coach: the review of an answer (ADR-0025). earlier: the
    # last answer to the same prompt and its review, or None.
    lists = "\n".join(f"- {name}: {what}" for name, what in criteria.items())
    again = "" if earlier is None else (
        f"This is a revision. The earlier answer and its review:\n{earlier}\n\n"
        "In strengths, also say what the revision improved.\n\n")
    return (
        "You coach a PhD student in scientific writing. Review the student's "
        "answer to a writing task.\n\n"
        f"Kind of writing: {kind}\nReader: {audience}\nWord limit: {max_words}\n"
        f"Task: {prompt}\n\nThe student's answer ({len(text.split())} words):\n{text}\n\n{again}"
        "In scores, give each criterion below a score from 0 to 10 (key: the "
        f"criterion name, copied exactly):\n{lists}\n\n"
        "Most answers of a capable student score 5 to 8; give 9 or 10 only to "
        "writing that a journal editor would not change. In strengths, write "
        "1 or 2 sentences on what works. In improvements, list the 2 or 3 "
        "changes that would improve this writer the most, most important "
        "first, each as an instruction with the reason. In edits, give 2 to 5 "
        "sentences or phrases of the answer: before (copied exactly), after "
        "(your version) and why (one short sentence). In revision, rewrite "
        "the whole answer as a strong writer would, within the word limit, "
        "with the student's content and voice."
    )


def interests_brief_prompt(library: str, searches: str) -> str:
    # The interests brief (ADR-0032); it is also the query of the pertinence.
    return (
        "Describe the research interests of a PhD student from the papers of "
        "the student's library. A star (*) marks a paper that the student "
        "rated crucial or recommended.\n\n"
        f"Library:\n{library}\n\n"
        f"The interest statements of the student's saved searches:\n{searches}\n\n"
        "Write one paragraph of 120 to 200 words in plain prose, to the "
        "student (\"Your library ...\"): the main topics, methods, data and "
        "clinical questions, the most important first. Use 8 to 15 keywords "
        "of interest in the prose, each in bold Markdown (**keyword**) at its "
        "first use. Use only what the titles and the statements show. Reply "
        "with the paragraph only."
    )


def triage_brief_prompt(interests: str, papers: str) -> str:
    # The triage brief (ADR-0032).
    return (
        "You help a PhD student choose what to read first.\n\n"
        f"The student's interests:\n{interests}\n\n"
        "These new papers wait in triage, the most pertinent first. "
        "Pertinence (0 to 1) is the match of the paper with the interests; "
        "triage (0 to 1) is a model's probability that the student wants to "
        "read the paper.\n\n"
        f"{papers}\n\n"
        "Write in Markdown. First, one or two sentences on what the papers in "
        "triage cover as a group. Then a numbered list of the 3 to 5 papers "
        "to read first, the best first: each as [P<id>] with a short title, "
        "then one or two sentences on why, from the abstract and the "
        "interests. Pertinence and triage are evidence, not the only reason. "
        "Use only facts from the abstracts. Reply with the Markdown only."
    )


def digest_brief_prompt(day: str, interests: str, papers: str) -> str:
    # The brief of one day of the digest (ADR-0032).
    return (
        "You write the summary of findings of a daily literature sweep for a "
        "PhD student.\n\n"
        f"The student's interests:\n{interests}\n\n"
        f"The relevant new papers that the sweep found on {day}, the most "
        "pertinent first. Pertinence (0 to 1) is the match of the paper with "
        "the interests; triage (0 to 1) is a model's probability that the "
        "student wants to read the paper.\n\n"
        f"{papers}\n\n"
        "Write in Markdown. First, one paragraph of 60 to 150 words: what "
        "happened in the field in these papers (the main themes, methods and "
        "results). Then a list of the 2 to 4 papers most pertinent to the "
        "student, each as [P<id>] with a short title and one sentence on why. "
        "Use only facts from the abstracts. Reply with the Markdown only."
    )


def puzzle_prompt(track: str, topic: str, puzzle_type: str, language: Optional[str]) -> str:
    # A new puzzle (ADR-0033). language: the language of a coding puzzle, or
    # None for a stats puzzle.
    if language is None:
        return (
            "You train a PhD student in medical image analysis with machine "
            "learning for oncology in statistics and machine learning. Write "
            "one puzzle that takes 10 to 30 minutes to answer in a few "
            "paragraphs, with numbers where they apply.\n\n"
            f"Topic: {topic}\nType: {puzzle_type}\n\n"
            "Use a concrete setting, from pathology or oncology where it fits, "
            "and give all the data that the answer needs. The puzzle must have "
            "one defensible answer, not only an opinion.\n\n"
            "In prompt, write the puzzle in Markdown. In reference, write a "
            "full model answer with the working. In key_points, list the 2 to "
            "5 points that a correct answer must contain. Leave starter and "
            "tests empty."
        )
    return (
        "You train a PhD student in medical image analysis with machine "
        f"learning for oncology in coding in {language}. Write one coding "
        "puzzle that takes 10 to 30 minutes.\n\n"
        f"Topic: {topic}\nType: {puzzle_type}\n\n"
        "The answer is one module solution.py; only the standard library, "
        "numpy, pandas and scipy are available, with no files and no network.\n\n"
        "In prompt, write the puzzle in Markdown: the function or functions to "
        "write, with their names, arguments and return values, and one or two "
        "examples. In starter, write the code that the student starts from: "
        "the signatures with a docstring (or, for Debug and Refactor, the "
        "code to change). In tests, write a test module: it starts with "
        "`from solution import ...`, and it has 4 to 8 functions named "
        "test_..., each with plain assert statements, which cover the normal "
        "cases and the edge cases. In reference, write a complete "
        "solution.py that passes every test. In key_points, list the 2 to 4 "
        "ideas that a good solution uses."
    )


def puzzle_grade_prompt(puzzle: dict, answer: str, results: Optional[list], known_topics: list[str]) -> str:
    # The score of an answer to a puzzle (ADR-0033). results: the test
    # results of a coding puzzle, or None.
    key_points = "\n".join(f"- {k}" for k in puzzle["key_points"])
    if results is None:
        evidence = ""
        rubric = ("Score the correctness and the reasoning: a correct answer "
                  "with the key points and clear working scores 8 to 10.")
    else:
        passed = sum(r["ok"] for r in results)
        lines = "\n".join(f"- {r['name']}: {'pass' if r['ok'] else 'FAIL ' + r['error']}" for r in results)
        evidence = f"The tests ran on the answer: {passed} of {len(results)} passed.\n{lines}\n\n"
        rubric = ("The tests decide most of the score: with a failed test, "
                  "give at most 6. Then judge the clarity, the idiom and the "
                  "efficiency of the code.")
    return (
        "You train a PhD student. Score the student's answer to this puzzle "
        "from 0 to 10.\n\n"
        f"Puzzle:\n{puzzle['prompt']}\n\n"
        f"Reference answer:\n{puzzle['reference']}\n\nKey points:\n{key_points}\n\n"
        f"{evidence}The student's answer:\n{answer}\n\n"
        f"{rubric} In feedback, write in Markdown: what is right, what is "
        "wrong or missing (with the reason), and the one change that would "
        "improve the answer most, with a short example if it helps. Do not "
        "repeat the reference answer in full." + review_topics_instruction(known_topics)
    )
