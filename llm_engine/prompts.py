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
