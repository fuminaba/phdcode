PAPER_METADATA_SCHEMA = {
    "title" : (str, ...),
    "authors" : (list[str], ...),
    "keywords" : (list[str], ...),
    "abstract" : (str, ...),
}

TRIAGE_SCHEMA = {
    "probability": (float, ...),
}

QUESTION_SCHEMA = {
    "question": (str, ...),
    "model_answer": (str, ...),
    "key_points": (list[str], ...),
    # One flat list; a list for each axis set is accepted too.
    "tags": (list[str] | dict[str, list[str]], ...),
}

GRADE_SCHEMA = {
    "score": (int, ...),
    "cited_papers": (list[int], ...),
    "feedback": (str, ...),
}
