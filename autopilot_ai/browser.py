"""Bounded, read-only Playwright browser access."""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field


class OpenWebpageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=2048)


class BrowserToolError(ValueError):
    pass


class BrowserPolicyError(BrowserToolError):
    pass


def _http_status_error(status: int) -> str:
    if status in (401, 403):
        return (
            f"The website returned HTTP {status} ({'Unauthorized' if status == 401 else 'Forbidden'}). "
            "It may require sign-in or block automated browser access. AutoPilot uses a fresh, unsigned browser session."
        )
    if status == 404:
        return "The website returned HTTP 404 (Not Found). Check the page address."
    if status >= 500:
        return f"The website returned HTTP {status} (server error). Try again later."
    return f"The page returned HTTP {status}."


def _validate_public_url(url: str, dns_cache: dict[str, tuple[str, ...]] | None = None) -> str:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            raise BrowserPolicyError("Only public HTTP or HTTPS URLs without embedded credentials are allowed.")
        if host.casefold() == "localhost" or host.casefold().endswith((".localhost", ".local", ".internal")):
            raise BrowserPolicyError("Local and internal hostnames are not allowed.")
        try:
            addresses = (str(ipaddress.ip_address(host)),)
        except ValueError:
            if dns_cache is not None and host in dns_cache:
                addresses = dns_cache[host]
            else:
                try:
                    addresses = tuple({item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)})
                except OSError as exc:
                    raise BrowserToolError(f"Could not resolve public web host {host}: {exc}") from exc
                if dns_cache is not None:
                    dns_cache[host] = addresses
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise BrowserPolicyError("URLs resolving to private, local, or reserved IP addresses are not allowed.")
        return url
    except ValueError as exc:
        if isinstance(exc, BrowserToolError):
            raise
        raise BrowserToolError(f"Invalid URL: {exc}") from exc


def open_webpage(url: str) -> dict:
    _validate_public_url(url)
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path(__file__).resolve().parents[1] / "down" / "playwright-browsers"))
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserToolError("Playwright is not installed. Install requirements and Chromium using the D: browser path in README.md.") from exc

    dns_cache: dict[str, tuple[str, ...]] = {}
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True, downloads_path=None, timeout=10_000)
        except PlaywrightTimeoutError as exc:
            raise BrowserToolError("Chromium did not start within 10 seconds.") from exc
        except PlaywrightError as exc:
            raise BrowserToolError(f"Could not start Chromium: {exc}") from exc
        try:
            context = browser.new_context(accept_downloads=False, java_script_enabled=True)
            page = context.new_page()

            def guard_request(route) -> None:
                if route.request.method not in ("GET", "HEAD"):
                    route.abort()
                    return
                try:
                    _validate_public_url(route.request.url, dns_cache)
                except BrowserToolError:
                    route.abort()
                else:
                    route.continue_()

            page.route("**/*", guard_request)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                if response is None:
                    raise BrowserToolError("The browser did not receive an HTTP response.")
                status = response.status
                if status >= 400:
                    raise BrowserToolError(_http_status_error(status))
                title = page.title()
                text = page.locator("body").inner_text(timeout=5_000)
            except PlaywrightTimeoutError as exc:
                raise BrowserToolError("The page did not finish loading within 20 seconds.") from exc
            except PlaywrightError as exc:
                raise BrowserToolError(f"Browser navigation failed: {exc}") from exc
            final_url = _validate_public_url(page.url, dns_cache)
            return {"requested_url": url, "url": final_url, "http_status": status, "title": title[:500], "text": text[:4000], "text_truncated": len(text) > 4000}
        finally:
            browser.close()
