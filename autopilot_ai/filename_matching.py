"""Shared deterministic filename matching for search and verification."""

from pathlib import Path


def matches_filename(name: str, query: str, match_mode: str, extensions: tuple[str, ...] | list[str] = ()) -> bool:
    """Match a basename by exact name, substring, or a requested suffix set."""
    if extensions:
        return Path(name).suffix.casefold() in {extension.casefold() for extension in extensions}
    folded_name = name.casefold()
    folded_query = query.casefold().strip()
    return folded_name == folded_query if match_mode == "exact" else folded_query in folded_name
