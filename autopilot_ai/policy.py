"""Path policy for filesystem tools."""

from pathlib import Path
from collections.abc import Iterable
import re


class PolicyError(ValueError):
    """Raised when a request crosses the configured filesystem boundary."""


_DESTRUCTIVE_COMMAND_PATTERNS = (
    r"\brmdir\b[^&|;]*[a-z]:\\",
    r"\bformat\b",
    r"\bdel\b[^&|;]*[a-z]:\\",
    r"\bdrop\s+database\b",
    r"\bshutdown\b",
    r"\bmkfs\b",
)
_SAFE_INSPECTION_COMMAND = re.compile(
    r"(?:dir(?:\s+[^&|;<>]*)?|echo\s+.+|ipconfig(?:\s+[^&|;<>]*)?|"
    r"git\s+status(?:\s+[^&|;<>]*)?|python\s+--version|pip\s+list(?:\s+[^&|;<>]*)?)\Z",
    re.IGNORECASE,
)


def command_risk(command: str) -> tuple[str, bool]:
    """Classify an allowed shell command without attempting to sanitize it."""
    normalized = " ".join(command.casefold().split())
    if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in _DESTRUCTIVE_COMMAND_PATTERNS):
        raise PolicyError("The command is blocked by the destructive-command policy.")
    if _SAFE_INSPECTION_COMMAND.fullmatch(normalized):
        return "low", False
    if any(token in normalized for token in ("&", "|", ";", ">", "<")):
        return "high", True
    return "medium", True


class PathPolicy:
    def __init__(self, approved_root: Path, additional_roots: Iterable[Path] = ()) -> None:
        try:
            primary = approved_root.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise PolicyError(f"Primary approved root cannot be resolved: {approved_root}") from exc
        if not primary.is_dir():
            raise PolicyError(f"Primary approved root is not a directory: {primary}")
        roots = [primary]
        for configured in additional_roots:
            try:
                root = configured.expanduser().resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise PolicyError(f"Approved search root cannot be resolved: {configured}") from exc
            if not root.is_dir():
                raise PolicyError(f"Approved search root is not a directory: {root}")
            if root not in roots:
                roots.append(root)
        self.approved_roots = tuple(roots)
        self.approved_root = primary

    def resolve(self, relative_path: str = ".", root_index: int = 0) -> Path:
        candidate = self.resolve_path(relative_path, root_index)
        if not candidate.exists():
            raise PolicyError("The requested workspace path does not exist.")
        if not candidate.is_dir():
            raise PolicyError("The requested workspace path is not a directory.")
        return candidate

    def resolve_path(self, relative_path: str, root_index: int = 0) -> Path:
        """Resolve an existing file or directory without following symlink components."""
        if not isinstance(root_index, int) or isinstance(root_index, bool):
            raise PolicyError("Approved root index must be an integer.")
        if root_index < 0 or root_index >= len(self.approved_roots):
            raise PolicyError("The requested approved root does not exist.")
        candidate_text = Path(relative_path)
        if candidate_text.is_absolute() or candidate_text.drive:
            raise PolicyError("Only relative paths inside approved directories are allowed.")
        if ".." in candidate_text.parts:
            raise PolicyError("Parent-directory traversal is not allowed.")

        selected_root = self.approved_roots[root_index]
        unresolved = selected_root / candidate_text
        current = selected_root
        for part in candidate_text.parts:
            if part in ("", "."):
                continue
            current = current / part
            if current.is_symlink():
                raise PolicyError("Symbolic links are not allowed in approved paths.")
        try:
            candidate = unresolved.resolve()
        except (OSError, RuntimeError) as exc:
            raise PolicyError("The requested path cannot be safely resolved.") from exc
        try:
            candidate.relative_to(selected_root)
        except ValueError as exc:
            raise PolicyError("The requested path resolves outside the selected approved directory.") from exc
        return candidate

    def resolve_new_path(self, relative_path: str, root_index: int = 0) -> Path:
        """Resolve a non-existing destination whose immediate parent already exists."""
        candidate_text = Path(relative_path)
        if not relative_path or candidate_text.name in ("", ".", ".."):
            raise PolicyError("A destination filename or directory name is required.")
        candidate = self.resolve_path(relative_path, root_index)
        if candidate.exists():
            raise PolicyError("The destination already exists; overwrite is not allowed.")
        parent = candidate.parent
        if not parent.is_dir():
            raise PolicyError("The destination parent must already exist inside an approved directory.")
        return candidate
