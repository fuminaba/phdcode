def json_schema_instruction(response_schema: dict) -> str:
    return (
        "Reply as a json string which can be directly passed into "
        f"pydantic, and matches the schema: {response_schema}. "
        "Do not include any extra text, only the required information."
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


def document_summary_prompt(document_text: str) -> str:
    return (
        "Summarize the following document using exactly this template, "
        "filling each section in based on its actual content. Use \"none\" "
        "for a section if it doesn't apply. Do not add extra sections, "
        "headers, or commentary outside the template.\n\n"
        "Topic: <one line>\n"
        "Key points: <bullet list>\n"
        "Core concepts / theoretical framing: <bullet list, or \"none\">\n"
        "Notable data or figures: <bullet list, or \"none\">\n\n"
        f"Document:\n{document_text}"
    )


def document_metadata_prompt(document_text: str) -> str:
    return (
        "Extract metadata from the following document. If it isn't a "
        "research article/paper, do your best with what's actually present "
        "— use an empty list for authors/keywords if none apply, rather "
        "than inventing them.\n\n"
        f"Document:\n{document_text}"
    )


def methods_summary_prompt(document_text: str) -> str:
    return (
        "Summarize the methodology used in the following document — study "
        "design, techniques, tools, procedures, data sources, sample sizes, "
        "etc. If the document doesn't describe a methodology, say so "
        "explicitly rather than inventing one.\n\n"
        f"Document:\n{document_text}"
    )


def results_summary_prompt(document_text: str) -> str:
    return (
        "Summarize the results/findings reported in the following document "
        "— key outcomes, measurements, statistical findings, and "
        "conclusions drawn from the data. If the document doesn't report "
        "results, say so explicitly rather than inventing them.\n\n"
        f"Document:\n{document_text}"
    )


