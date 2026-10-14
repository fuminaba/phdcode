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


def question_prompt(papers: str, topics: tuple[str, ...]) -> str:
    # The daily question (ADR-0020).
    return (
        "You examine a PhD student in medical image analysis with machine "
        "learning for oncology. Write one question that the student should be "
        "able to answer in one paragraph after reading these papers. Ask about "
        "an idea, a method, a design choice, a limitation or a comparison, not "
        "about a number or a detail that only memory gives. Prefer a question "
        "that needs more than one of the papers. Do not name the papers in the "
        "question.\n\n"
        "Then write the model answer: one paragraph that cites the papers by "
        "their ids in brackets, for example [P12].\n\n"
        "In topics, list the one or two topics of the question, copied "
        f"exactly from this list: {'; '.join(topics)}.\n\n"
        f"Papers:\n{papers}"
    )


def grade_prompt(question: str, model_answer: str, papers: str, library: str, answer: str) -> str:
    # The grade of the daily question (ADR-0020).
    return (
        "You grade the answer of a PhD student to an exam question.\n\n"
        f"Question: {question}\n\nModel answer: {model_answer}\n\n"
        f"The papers of the question:\n{papers}\n\n"
        f"The student's library (id: title, first author, year):\n{library}\n\n"
        f"The student's answer: {answer}\n\n"
        "Give a score from 0 to 10 for correctness, depth and clarity; a "
        "different correct answer than the model answer is fine. List in "
        "cited_papers the id numbers (12 for P12) of the library papers that "
        "the answer cites, by title, author, year or id, where the paper "
        "supports the claim. Leave out a citation that is wrong. In feedback, "
        "write 2 to 4 sentences: what was right, what was missing or wrong."
    )
