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
    "topics": (list[str], ...),
}

GRADE_SCHEMA = {
    "score": (int, ...),
    "cited_papers": (list[int], ...),
    "feedback": (str, ...),
}
