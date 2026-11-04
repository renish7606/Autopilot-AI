# AutoPilot AI

AutoPilot AI is a local-first autonomous computer-use agent, developed in verified phases. The current CLI can plan and execute bounded filesystem and read-only browser tasks, and inspect the active Windows window; desktop actions, calendar integration, task memory, and a dashboard remain future phases.

## Current phase

The current runnable slice accepts a natural-language request, asks one local Ollama model for a schema-constrained plan by default, validates each proposal against deterministic intent, registered tools, and policy, and verifies results. Read-only directory listing and recursive filename search can be combined in sequences of up to three steps; each is verified before the next starts. Controlled file changes include creating a directory and copying a file within the primary workspace; both require `--approve`, reject path escapes and existing destinations, and are independently verified. Playwright opens public HTTP/HTTPS pages in a fresh context and verifies them in another. Windows UI Automation can inspect the foreground window; if UIA fails, PyAutoGUI saves a screenshot under `D:\Agent\down\screenshots`. Read-only tools get one retry after an execution error or verification mismatch; file changes are never automatically repeated. Unsupported model plans fall back to deterministic intent parsing. The CLI is the initial interface; React is intentionally deferred.

The local planner connects to Ollama at `http://127.0.0.1:11434` and defaults to `qwen2.5:0.5b`; override it with `AUTOPILOT_MODEL`. The development setup has Qwen installed under `D:\Agent\down\models`. Keep Ollama running to use model planning; supported requests can fall back to the deterministic planner if Ollama is unavailable or returns a plan that does not match the request. The observed machine profile was 7.7 GiB RAM, 0.84 GiB available RAM, a 12th Gen Intel Core i5-1235U with 12 logical CPUs, and 196 GiB free on D:. Runtime headroom should still be measured before choosing a larger model.

## Run

Requires Python 3.10+ and Pydantic 2. In PowerShell, first change to the project directory so pytest and Python only see this project:

```powershell
Set-Location D:\Agent
py -m venv .venv
.venv\Scripts\Activate.ps1
$env:PIP_CACHE_DIR = "D:\Agent\down\pip-cache"
$env:PLAYWRIGHT_BROWSERS_PATH = "D:\Agent\down\playwright-browsers"
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m autopilot_ai.cli "Show me the files in this folder"
```

Playwright uses a fresh, headless Chromium context for each page. Browser binaries are kept under `D:\Agent\down\playwright-browsers`; the application sets this location by default. It only opens public HTTP/HTTPS pages, blocks localhost/private-network destinations, downloads, and requests other than GET/HEAD, and returns visible page text (up to 4,000 characters). It does not submit forms or reuse your personal browser profile.

Desktop inspection uses pywinauto with the Windows UI Automation (`uia`) backend. If the active application does not expose usable UIA controls, the tool captures a screenshot with PyAutoGUI. Screenshots are saved under `D:\Agent\down\screenshots` and can include anything visible on screen. This phase does not click, type, or launch applications.

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

To open a public web page and inspect its visible text:

```powershell
python -m autopilot_ai.cli "Open https://example.com" --planner heuristic
```

To inspect the foreground desktop window, or switch to another app before the check:

```powershell
python -m autopilot_ai.cli "Inspect active window" --planner heuristic
python -m autopilot_ai.cli "Inspect active window" --planner heuristic --delay-seconds 5
```

The delay pauses for five seconds after planning and before inspection. Use that time to switch focus to the app you want inspected. Without a delay, the terminal or IDE running the command will usually remain the foreground window.

File changes are previewed first. Review the `plan` in the output, then repeat the request with `--approve` to authorize it. Supported changes are creating one new directory or copying one file to a new destination under the primary workspace. Copy sources may be relative to any approved root by setting `source_root_index` in a direct tool call; the natural-language CLI currently uses the primary root.

```powershell
python -m autopilot_ai.cli "Create folder reports" --planner heuristic
python -m autopilot_ai.cli "Create folder reports" --planner heuristic --approve
```

To try copying, first make a sample source file. Then preview and approve the copy. The source and new destination are both relative to `D:\Agent\workspace`:

```powershell
Set-Content .\workspace\source.txt "sample content"
python -m autopilot_ai.cli "Copy file source.txt to backup.txt" --planner heuristic
python -m autopilot_ai.cli "Copy file source.txt to backup.txt" --planner heuristic --approve
Get-Content .\workspace\backup.txt
```

The `python` commands assume the `.venv` created above is activated. Otherwise prefix each command with the virtual environment's Python executable so it uses the environment where Playwright was installed. `--approve-dir` only adds folders to filename search; it does not authorize writing there.

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

The tests cover execution and independent verification for listing, search, directory creation, copying, browser page results, and desktop inspection; bounded retries; approval gating; no-overwrite behavior; URL restrictions; filename-only search; structured Ollama planning; deterministic fallback; unregistered tool denial; traversal and symlink escapes where supported; and unsupported goals. Browser and desktop tests use mocked observations and do not interact with the network or current desktop. On this machine, the global pytest install has a broken optional `langsmith` plugin; run with `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"` before `python -m pytest` to avoid loading unrelated global plugins.

## Roadmap

Build one phase at a time: hardware and runtime setup; local model adapter and structured tool selection; bounded multi-step planning with Ollama; controlled file operations (create directory and copy file); Playwright browser access; Windows UI Automation with PyAutoGUI screenshot fallback; bounded recovery (current phase); persistent approvals and SQLite task memory; Google Calendar OAuth and approval policy; then the React dashboard and broader evaluation. Do not expose unrestricted shell execution or claim task success without verification.

## Safety boundary

Registered tools are allowlisted and validate arguments with Pydantic. Search checks filename matches only, skips symlinks, reports inaccessible locations or limits as incomplete, and returns full paths. Create and copy operate only in the primary approved workspace and never overwrite an existing destination; they require `--approve`. Browser access is read-only and rejects local/private destinations. Desktop access only reads accessibility information or captures a screenshot; it does not click, type, launch apps, or execute commands. This phase does not implement deletion, calendar changes, desktop actions, task memory, or arbitrary command execution.
