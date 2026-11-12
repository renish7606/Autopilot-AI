"""Bounded DuckDuckGo text search with no API key or browser session."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchWebArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str = Field(min_length=1, max_length=200)
    max_results: int = Field(default=5, ge=1, le=10)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Search query cannot be blank.")
        return normalized


class WebSearchError(RuntimeError):
    """The external search service could not return usable results."""


def search_web(query: str, max_results: int = 5) -> dict:
    """Return a small, normalized set of public web search results."""
    args = SearchWebArgs.model_validate({"query": query, "max_results": max_results})
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise WebSearchError("Web search needs the 'ddgs' package. Install project dependencies with: python -m pip install -r requirements.txt") from exc
    try:
        raw_results = DDGS(timeout=10).text(args.query, max_results=args.max_results, backend="auto")
    except Exception as exc:
        # ddgs raises this exact exception when its backend completed without
        # any hits; that is a valid empty search response, unlike a timeout or
        # provider/rate-limit error.
        is_empty_results = (
            exc.__class__.__module__ == "ddgs.exceptions"
            and exc.__class__.__name__ == "DDGSException"
            and str(exc).strip().casefold() == "no results found."
        )
        if is_empty_results:
            raw_results = []
        else:
            raise WebSearchError(f"DuckDuckGo search failed: {exc}") from exc
    if raw_results is None:
        raise WebSearchError("DuckDuckGo returned no result payload.")

    results = []
    seen_urls: set[str] = set()
    try:
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()[:500]
            url = str(item.get("href") or item.get("url") or "").strip()[:2048]
            snippet = str(item.get("body") or item.get("snippet") or "").strip()[:1200]
            parsed = urlsplit(url)
            if not title or parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                continue
            host = parsed.hostname.rstrip(".").casefold()
            if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
                continue
            try:
                if not ipaddress.ip_address(host).is_global:
                    continue
            except ValueError:
                if "." not in host:
                    continue
            key = url.casefold()
            if key in seen_urls:
                continue
            seen_urls.add(key)
            results.append({"rank": len(results) + 1, "title": title, "url": url, "snippet": snippet})
            if len(results) >= args.max_results:
                break
    except Exception as exc:
        raise WebSearchError(f"Could not normalize DuckDuckGo results: {exc}") from exc
    return {"query": args.query, "provider": "DDGS metasearch", "results": results, "count": len(results)}
