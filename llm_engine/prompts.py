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


def grade_prompt(question: str, model_answer: str, key_points: list[str], papers: str, library: str,
                 answer: str) -> str:
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
    )
