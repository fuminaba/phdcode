JUDGE_SCHEMA = {
    "passed" : (bool, ...),
    "score" : (int, ...),   # 1-10
    "feedback" : (str, ...),
}

DOCUMENT_METADATA_SCHEMA = {
    "title" : (str, ...),
    "authors" : (list[str], ...),
    "keywords" : (list[str], ...),
}
