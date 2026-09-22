import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()

TRANSCRIPTS_DIR = Path(os.getenv("LOG_DIR", "logs")) / "conversations"


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" for line in text.splitlines())


def _format_turn(
        user_prompt: str,
        result: dict[str, Any],
        model_name: Optional[str]
) -> str:
    heading = f"## {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    if model_name:
        heading += f" — `{model_name}`"

    lines = [heading, "", "**User:**", _blockquote(user_prompt), ""]

    evaluation = result.get("evaluation")
    if evaluation is not None:
        verdict = "passed" if evaluation.get("passed") else "failed"
        lines.append(
            f"**Evaluation:** {verdict} (score {evaluation.get('score')}/10) "
            f"— {evaluation.get('feedback')}"
        )
        lines.append("")

    output = result.get("output")
    output_text = output if isinstance(output, str) else json.dumps(output, indent=2)
    lines += ["**Assistant:**", _blockquote(output_text), ""]

    tokens = result.get("tokens_used")
    if tokens is not None:
        lines += [f"_Tokens used: {tokens}_", ""]

    lines += ["---", ""]
    return "\n".join(lines) + "\n"


class ConversationTranscript:
    """Human-readable markdown log of one conversation: every user turn, its
    evaluation (if any), and its final answer. This file is the
    transparent, human-readable summary; the memory database separately
    keeps a JSONB backup of the same turns (plus a summary + embedding for
    search), and the detailed step-by-step history (every LLM/tool call,
    live as it happens, not just once a turn completes) lives in
    turn_actions -- see memory.db.write_turn_action -- not duplicated here.

    Conversation boundaries (when to start a new file vs. keep appending)
    are the caller's decision: pass `path` to continue an existing
    transcript, or omit it to start a fresh one.

    user_id/chat_id/conversation_id are recorded in both the filename and
    the file header so a transcript can be identified (and a user's
    transcripts found) without touching the database — user_id today is
    just the Telegram user id, standing in until real auth exists.
    """

    def __init__(
            self,
            user_id: str,
            chat_id: str,
            conversation_id: int,
            path: Optional[Path] = None,
    ):
        self.user_id = user_id
        self.chat_id = chat_id
        self.conversation_id = conversation_id

        if path is not None:
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
        else:
            user_dir = TRANSCRIPTS_DIR / str(user_id)
            user_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d %Hh-%Mm")
            self.path = user_dir / f"conv{conversation_id}_chat{chat_id}_{timestamp}.md"

        if not self.path.exists():
            self.path.write_text(
                f"# Conversation {conversation_id}\n\n"
                f"User: `{self.user_id}`  \n"
                f"Chat: `{self.chat_id}`  \n"
                f"Started: {datetime.now().isoformat(timespec='seconds')}\n\n"
                "---\n\n"
            )

    def _append_sync(self, text: str) -> None:
        with self.path.open("a") as f:
            f.write(text)

    async def append_turn(
            self,
            user_prompt: str,
            result: dict[str, Any],
            model_name: Optional[str] = None,
    ) -> None:
        """Formats and appends one turn. Runs the actual file write in a
        thread so it doesn't block the event loop."""
        text = _format_turn(user_prompt, result, model_name)
        await asyncio.to_thread(self._append_sync, text)
