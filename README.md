# AutoPilot AI

AutoPilot AI is a local-first computer-use agent project. The first vertical slice proves the execution boundary before adding browser, desktop, calendar, global search, memory, or a dashboard.

## Current phase

The current runnable slice accepts a natural-language request, asks one local Ollama model for a schema-constrained plan by default, validates the proposal against a deterministic intent check, registered tools, and approved-root policy, executes a read-only directory listing or recursive filename search, and independently rescans the filesystem before it reports completion. If the model cannot plan a request that the bounded deterministic planner supports, the agent uses that plan and labels the fallback in its report. Arbitrary tools and paths outside approved roots are denied. Search is case-insensitive and exact by default; users can ask for a partial filename match. It returns at most 500 results and stops after scanning 100,000 entries across all roots. Use `--planner heuristic` to run the deterministic planner without a model. The CLI is the initial interface; React is intentionally deferred.

The local planner connects to Ollama at `http://127.0.0.1:11434` and defaults to `qwen2.5:0.5b`; override it with `AUTOPILOT_MODEL`. Ollama is present on the development machine, but the local server is stopped and its startup currently fails while trying to write logs under the restricted C: profile. A local model has not yet been downloaded or benchmarked. The observed machine profile was 7.7 GiB RAM, 0.84 GiB available RAM, a 12th Gen Intel Core i5-1235U with 12 logical CPUs, and 196 GiB free on D:. The 0.5B model is a small candidate, not a finalized selection. Keep model downloads under `D:\Agent\down\models` and benchmark runtime headroom before relying on it.

## Run

Requires Python 3.10+ and Pydantic 2. In PowerShell, first change to the project directory so pytest and Python only see this project:

```powershell
Set-Location D:\Agent
py -m venv .venv
.venv\Scripts\Activate.ps1
$env:PIP_CACHE_DIR = "D:\Agent\down\pip-cache"
python -m pip install -r requirements.txt
python -m autopilot_ai.cli "Show me the files in this folder"
```

By default, the approved workspace is `workspace/`. Set `--workspace` to select a different primary root. For filename search, approve directories once and then search by filename alone. The local allowlist is stored in `data/approved_directories.json` on D: and can be inspected or cleared. `--approved-dir` remains available for a one-time root. Listing is limited to the primary root; filename search checks every approved root.

Approve, inspect, or clear search roots:

```powershell
python -m autopilot_ai.cli --approve-dir "C:\Users\ASUS\Downloads" --approve-dir "D:\Agent\down"
python -m autopilot_ai.cli --list-approved-dirs
python -m autopilot_ai.cli --unapprove-dir "D:\Agent\down"
python -m autopilot_ai.cli --clear-approved-dirs
```

After approving directories, pass only the filename:

```powershell
python -m autopilot_ai.cli "AutoPilot_AI_Project_Blueprint.pdf"
```

To list entries:

```powershell
python -m autopilot_ai.cli "Show me the files in this folder"
```

To search filenames recursively:

```powershell
python -m autopilot_ai.cli "Find files containing budget"
```

For a one-time exact filename search across Downloads and another folder, you can pass a natural-language request or a bare filename. Results include the full path for every match:

```powershell
python -m autopilot_ai.cli "AutoPilot_AI_Project_Blueprint.pdf" `
  --approved-dir "C:\Users\ASUS\Downloads" `
  --approved-dir "D:\Agent\down"
```

For local-model execution, set the Ollama model directory to D: before starting its server and downloading the candidate model:

```powershell
$env:OLLAMA_MODELS = "D:\Agent\down\models"
ollama serve
# In a second PowerShell window, set the same variable, then:
ollama pull qwen2.5:0.5b
```

If Ollama is already running in the background, stop it and start it from a shell with that variable set so model files go to D:. Deterministic fallback: `python -m autopilot_ai.cli "Show me the files in this folder" --planner heuristic`.

## Verify this phase

```powershell
Set-Location D:\Agent
python -m pytest
```

The tests cover execution and independent verification for listing and search, filename-only search behavior, structured Ollama planning, deterministic fallback for supported requests, unregistered tool denial, parent traversal, absolute paths, symlink escapes where supported, and unsupported goals. On this machine, the global pytest install has a broken optional `langsmith` plugin; run with `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"` before `python -m pytest` to avoid loading unrelated global plugins.

## Roadmap

Build one phase at a time: hardware and runtime setup; local model adapter and structured tool selection; bounded multi-step planning; multiple explicitly approved search roots and controlled file operations; Playwright; Windows UI Automation with PyAutoGUI fallback; bounded recovery; approvals and SQLite task memory; Google Calendar OAuth and approval policy; then the React dashboard and broader evaluation. Do not expose unrestricted shell execution or claim task success without verification.

## Safety boundary

Only `list_directory` and `search_files` are registered. Both are read-only and validate arguments with Pydantic. Search checks filename matches only, skips symlinks, reports inaccessible locations or limits as incomplete, and returns full paths. It does not read file contents. This phase does not implement file writes, calendar changes, browser actions, desktop controls, task memory, or arbitrary command execution.
