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

DAILY_PAPER_SCHEMA = {
    "about": (str, ...),
    "summary": (str, ...),
    "key_points": (list[str], ...),
    "examiner_questions": (list[str], ...),
}

WRITING_PROMPT_SCHEMA = {
    "prompt": (str, ...),
    "audience": (str, ...),
    "max_words": (int, ...),
}

WRITING_REVIEW_SCHEMA = {
    "scores": (dict[str, int], ...),
    "strengths": (str, ...),
    "improvements": (list[str], ...),
    "edits": (list[dict[str, str]], ...),
    "revision": (str, ...),
}
