"""Path policy for filesystem tools."""

from pathlib import Path
from collections.abc import Iterable


class PolicyError(ValueError):
    """Raised when a request crosses the configured filesystem boundary."""


class PathPolicy:
    def __init__(self, approved_root: Path, additional_roots: Iterable[Path] = ()) -> None:
        primary = approved_root.expanduser().resolve(strict=True)
        if not primary.is_dir():
            raise PolicyError(f"Primary approved root is not a directory: {primary}")
        roots = [primary]
        for configured in additional_roots:
            root = configured.expanduser().resolve(strict=True)
            if not root.is_dir():
                raise PolicyError(f"Approved search root is not a directory: {root}")
            if root not in roots:
                roots.append(root)
        self.approved_roots = tuple(roots)
        self.approved_root = primary

    def resolve(self, relative_path: str = ".", root_index: int = 0) -> Path:
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
        candidate = (selected_root / candidate_text).resolve()
        try:
            candidate.relative_to(selected_root)
        except ValueError as exc:
            raise PolicyError("The requested path resolves outside the selected approved directory.") from exc
        if not candidate.exists():
            raise PolicyError("The requested workspace path does not exist.")
        if not candidate.is_dir():
            raise PolicyError("The requested workspace path is not a directory.")
        return candidate
