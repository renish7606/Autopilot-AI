"""Launch the Autopilot AI web server with ``python -m autopilot_ai.web``."""

from pathlib import Path


def main() -> None:
    import uvicorn

    from .server import app

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
        print("Serving the built dashboard and API.")
    else:
        print("Dashboard assets are not built; use the Vite dev server for the frontend.")

    host = "127.0.0.1"
    port = 8000
    print(f"Autopilot AI server starting at http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
