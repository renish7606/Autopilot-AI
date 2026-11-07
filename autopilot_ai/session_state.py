"""Small in-memory state for a future conversational CLI turn.

This deliberately has no persistence: callers own one instance for the lifetime
of a conversation and may discard it to clear all pending context.
"""

from dataclasses import dataclass, field
import re


_ORDINALS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
}


@dataclass
class SessionState:
    """Retain one pending clarification and its ordered candidate strings."""

    pending_question: str | None = None
    candidates: tuple[str, ...] = field(default_factory=tuple)

    def set_pending(self, question: str, candidates: list[str] | tuple[str, ...] = ()) -> None:
        question = question.strip()
        if not question:
            raise ValueError("A pending question cannot be empty.")
        if any(not isinstance(candidate, str) or not candidate.strip() for candidate in candidates):
            raise ValueError("Candidates must be non-empty strings.")
        self.pending_question = question
        self.candidates = tuple(candidates)

    def select_candidate(self, reply: str) -> str | None:
        """Resolve replies such as '2' or 'the second one'; leave state on mismatch."""
        if self.pending_question is None:
            return None
        index = _candidate_index(reply)
        if index is None or index < 1 or index > len(self.candidates):
            return None
        selected = self.candidates[index - 1]
        self.clear()
        return selected

    def is_candidate_reply(self, reply: str) -> bool:
        """Tell a syntactically numbered reply from a new natural-language task."""
        return self.pending_question is not None and _candidate_index(reply) is not None

    def clear(self) -> None:
        """Forget pending clarification and candidates."""
        self.pending_question = None
        self.candidates = ()


def _candidate_index(reply: str) -> int | None:
    if not isinstance(reply, str):
        return None
    normalized = reply.strip().casefold()
    match = re.fullmatch(
        r"(?:the\s+)?(\d+(?:st|nd|rd|th)?|first|second|third|fourth|fifth)(?:\s+(?:one|file|folder|result))?[.!]?",
        normalized,
    )
    if not match:
        return None
    token = match.group(1)
    if token[0].isdigit():
        return int(re.match(r"\d+", token).group())
    return _ORDINALS[token]
