"""Converts the model's CommonMark-ish markdown output into the HTML
subset Telegram's parse_mode="HTML" actually supports (b/i/u/s/code/pre/
a/blockquote -- no headers, no native lists), so replies render with real
bold/italic/code instead of literal asterisks the user has to read past.

HTML over MarkdownV2 deliberately: MarkdownV2 requires escaping a dozen
characters (_*[]()~`>#+-=|{}.!) whenever they're NOT part of intended
syntax, which is fragile to generate correctly from a model's own
freeform markdown. HTML only ever requires escaping three (&<>), so it's
far more reliable to produce programmatically -- see _markdown_to_html's
docstring for the exact algorithm.
"""
import html
import re

_CODE_BLOCK_RE = re.compile(r"```(?:\w*\n)?(.*?)```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+?)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!_)_([^_\n]+?)_(?!_)")
_HEADER_RE = re.compile(r"^#{1,6}[ \t]+(.+)$", re.MULTILINE)
_TAG_RE = re.compile(r"<[^>]+>")


def markdown_to_telegram_html(text: str) -> str:
    """Order matters: code spans/blocks are protected FIRST via
    placeholders, since their content must never be touched by a later
    substitution or escaped a second time. Links come next, extracted
    while their URL/label are still raw so escaping can be applied
    precisely to each half. Only then is the REMAINING prose
    html-escaped, so a literal '<' or '&' in the model's own text can't
    be misread as a tag -- and only after that do headers/bold/italic
    turn into real tags, safely, since nothing left in the string can be
    accidentally interpreted as markup by the escape step (it already
    ran). Bold before italic, so **x** isn't first read as two italics.

    Plain '- item' bullets are left as literal text -- Telegram's HTML
    has no list tags, and hyphen bullets already read fine unconverted.
    """
    placeholders: list[str] = []

    def stash(html_snippet: str) -> str:
        placeholders.append(html_snippet)
        return f"\x00{len(placeholders) - 1}\x00"

    text = _CODE_BLOCK_RE.sub(lambda m: stash(f"<pre>{html.escape(m.group(1))}</pre>"), text)
    text = _INLINE_CODE_RE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)

    def link(m: re.Match) -> str:
        label = html.escape(m.group(1))
        url = html.escape(m.group(2), quote=True)
        return stash(f'<a href="{url}">{label}</a>')
    text = _LINK_RE.sub(link, text)

    text = html.escape(text)

    text = _HEADER_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD_RE.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC_RE.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)

    for i, snippet in enumerate(placeholders):
        text = text.replace(f"\x00{i}\x00", snippet)

    return text


def strip_telegram_html(text: str) -> str:
    """Recovers plain, readable text from an HTML chunk -- the fallback
    path when Telegram rejects a chunk as malformed HTML (should be rare;
    see webhook_server._send_formatted), so a formatting bug degrades to
    plain text instead of losing the message."""
    return html.unescape(_TAG_RE.sub("", text))
