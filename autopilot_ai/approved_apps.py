"""Persistent per-application launch approvals."""

import json
from pathlib import Path


DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "data" / "approved_apps.json"


class ApprovedAppError(ValueError):
    pass


def _key(app_name: str) -> str:
    normalized = " ".join(app_name.casefold().split())
    if not normalized:
        raise ApprovedAppError("Application name cannot be empty.")
    return normalized


def load_approved_apps(config_path: Path = DEFAULT_CONFIG) -> set[str]:
    if not config_path.exists():
        return set()
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApprovedAppError(f"Could not read approved-application list: {exc}") from exc
    if not isinstance(payload, list) or any(not isinstance(item, str) for item in payload):
        raise ApprovedAppError("Approved-application list must be a JSON array of application names.")
    return {_key(item) for item in payload}


def approve_app(app_name: str, config_path: Path = DEFAULT_CONFIG) -> None:
    apps = load_approved_apps(config_path)
    apps.add(_key(app_name))
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary.write_text(json.dumps(sorted(apps), indent=2), encoding="utf-8")
        temporary.replace(config_path)
    except OSError as exc:
        raise ApprovedAppError(f"Could not save approved-application list: {exc}") from exc


def is_app_approved(app_name: str, config_path: Path = DEFAULT_CONFIG) -> bool:
    return _key(app_name) in load_approved_apps(config_path)
