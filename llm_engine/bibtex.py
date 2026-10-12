"""A small BibTeX reader for `phdcode import` (a Zotero or Better BibTeX
export): entries with braced, quoted or bare field values. No @string or
@comment support; Zotero does not write them."""
import re

_ENTRY = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,")
_FIELD = re.compile(r"\s*([\w-]+)\s*=\s*")
_LATEX = [(r"\&", "&"), (r"\%", "%"), (r"\_", "_"), (r"\$", "$"), ("~", " "), ("--", "–")]


def _value(text: str, i: int) -> tuple[str, int]:
    """The value that starts at text[i] and the index after it."""
    if text[i] in "{\"":
        close = "}" if text[i] == "{" else "\""
        depth, j = 0, i
        while j < len(text):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}" and close == "}":
                depth -= 1
                if depth == 0:
                    return text[i + 1:j], j + 1
            elif c == "\"" and close == "\"" and j > i and depth == 0:
                return text[i + 1:j], j + 1
            elif c == "}":
                depth -= 1
            j += 1
        raise ValueError(f"An unclosed value at character {i}.")
    m = re.match(r"[^,}\s]+", text[i:])
    return m.group(0), i + m.end()


def clean(value: str) -> str:
    """Removes the case-protection braces and the common LaTeX escapes."""
    value = value.replace("{", "").replace("}", "")
    for latex, plain in _LATEX:
        value = value.replace(latex, plain)
    return " ".join(value.split())


def parse(text: str) -> list[dict[str, str]]:
    """Each entry as a dict: "type", "key", and each field (lower-case name),
    cleaned."""
    entries = []
    for m in _ENTRY.finditer(text):
        entry = {"type": m.group(1).lower(), "key": m.group(2)}
        i = m.end()
        while True:
            field = _FIELD.match(text, i)
            if not field:
                break
            value, i = _value(text, field.end())
            entry[field.group(1).lower()] = clean(value)
            i = re.match(r"\s*,?", text[i:]).end() + i
        entries.append(entry)
    return entries


if __name__ == "__main__":
    sample = """
@article{agosti2024,
  title = {Histopathological Evaluation: {{Current}} Issues},
  author = {Agosti, Vittorio and Munari, Enrico},
  year = 2024,
  month = nov,
  doi = {10.4103/aja202440},
  abstract = {A {nested {brace}} and 50\\% of \\& cases},
  pmid = {39254403}
}
@misc{leopard, title = "The {LEOPARD} Challenge", note = {x}}
"""
    a, b = parse(sample)
    assert a["type"] == "article" and a["key"] == "agosti2024"
    assert a["title"] == "Histopathological Evaluation: Current Issues", a["title"]
    assert a["year"] == "2024" and a["month"] == "nov" and a["pmid"] == "39254403"
    assert a["abstract"] == "A nested brace and 50% of & cases", a["abstract"]
    assert b == {"type": "misc", "key": "leopard", "title": "The LEOPARD Challenge", "note": "x"}, b
    print("bibtex: ok")
