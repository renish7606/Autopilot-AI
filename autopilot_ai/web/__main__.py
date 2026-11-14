"""Launch the Autopilot AI web server with ``python -m autopilot_ai.web``."""

from pathlib import Path


def main() -> None:
    import uvicorn

    from .server import app

    index_file = Path(__file__).resolve().parents[2] / "frontend" / "dist" / "index.html"
    if index_file.is_file():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(index_file.parent), html=True), name="frontend")
        print("Mode: production (serving the built dashboard and API).", flush=True)
    else:
        print("Mode: development (dashboard build not found; run the Vite dev server separately).", flush=True)

    host = "127.0.0.1"
    port = 8000
    print(f"Autopilot AI server starting at http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
