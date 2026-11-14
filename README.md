# AutoPilot AI

AutoPilot AI is a local-first autonomous computer-use agent, developed in verified phases. The CLI and local web dashboard can plan and execute bounded filesystem tasks, read approved file contents, run selected terminal commands, navigate read-only browser tasks, launch installed Windows applications, inspect and perform approved actions in the active Windows window, and persist task history across sessions. Calendar integration remains a future phase.

## Current phase

The current runnable slice accepts a natural-language request, asks one local Ollama model for a schema-constrained plan by default, validates each proposal against deterministic intent, registered tools, and policy, and verifies results. Read-only directory listing and recursive filename search can be combined in sequences of up to three steps; each is verified before the next starts. Approved text, code, Markdown, JSON/CSV, and PDF files can be read with a character limit, and their returned slice and SHA-256 digest are independently checked with a fresh disk read. Controlled file changes include creating a directory, copying a file, and creating or updating a text file within an approved workspace; each requires `--approve`, rejects path escapes, and is independently verified. Terminal commands run only from an approved working directory; destructive commands are blocked, and non-inspection commands require `--approve`. An application launch requires `--approve` once per application name; successful approvals are stored locally and later launches of that same application run directly. Launches are verified from the Windows process list. Playwright opens public HTTP/HTTPS pages in a fresh context and verifies them in another. Web search uses the no-key `ddgs` metasearch package and validates query, ranking, result structure, and link format; it returns titles and provider snippets, not a generated summary. Windows UI Automation can inspect the foreground window; if UIA fails, PyAutoGUI saves a screenshot under `D:\Agent\down\screenshots`. Eligible read-only local, browser, and desktop tools can get one bounded retry after transient execution errors or failed verification; web search does not retry to avoid compounding provider rate limits. File changes, terminal commands, and application launches are never automatically repeated. Unsupported model plans fall back to deterministic intent parsing. The CLI and local React dashboard are the available interfaces.

The local planner connects to Ollama at `http://127.0.0.1:11434` and defaults to `qwen2.5:0.5b`; override it with `AUTOPILOT_MODEL`. The development setup has Qwen installed under `D:\Agent\down\models`. Keep Ollama running to use model planning; supported requests can fall back to the deterministic planner if Ollama is unavailable or returns a plan that does not match the request. The observed machine profile was 7.7 GiB RAM, 0.84 GiB available RAM, a 12th Gen Intel Core i5-1235U with 12 logical CPUs, and 196 GiB free on D:. Runtime headroom should still be measured before choosing a larger model.

## Run

For a PowerShell command walkthrough of each current feature and its verification steps, see [`run.txt`](run.txt).

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

Playwright uses a fresh, headless Chromium context for each page. Browser binaries are kept under `D:\Agent\down\playwright-browsers`; the application sets this location by default. It only opens public HTTP/HTTPS pages, blocks localhost/private-network destinations, downloads, and requests other than GET/HEAD, and returns visible page text (up to 4,000 characters). It does not submit forms or reuse your personal browser profile. Sites can refuse automated requests or require sign-in; those responses are reported as failures, and AutoPilot cannot use a session from your regular browser.

Web search is a separate read-only capability. It uses the `ddgs` package (the `duckduckgo-search` project was renamed) and its automatic metasearch backend; no API key is required. Even when a request says “Search Google,” AutoPilot does not claim Google supplied the result. Returned snippets are supplied by the search provider and are not a locally generated summary; use `open https://...` as a separate request to read a public result page.

Desktop inspection uses pywinauto with the Windows UI Automation (`uia`) backend. If UIA fails, the fallback captures only the visible region of the foreground window on the primary display with PyAutoGUI. Screenshots are saved under `D:\Agent\down\screenshots`; they may still contain sensitive information shown inside that application. Verification checks the image dimensions and that the same window remains active. Separate mouse and keyboard actions require `--approve`, are limited to one explicit action per request, and verify screen bounds, cursor position, or foreground-window identity.

By default, the approved workspace is `workspace/`. Set `--workspace` to select a different primary root. For filename search and file reading, approve directories once and then use filenames from those roots. The local allowlist is stored in `data/approved_directories.json` on D: and can be inspected or cleared. `--approved-dir` remains available for a one-time root. Listing is limited to the primary root; filename search checks every approved root.

Approve, inspect, or clear search roots:

```powershell
python -m autopilot_ai.cli --approve-dir "C:\Users\ASUS\Downloads" --approve-dir "D:\Agent\down"
python -m autopilot_ai.cli --list-approved-dirs
python -m autopilot_ai.cli --unapprove-dir "D:\Agent\down"
python -m autopilot_ai.cli --clear-approved-dirs
```

Manage application launch approvals separately. These flags persist normalized application names in `data/approved_apps.json`; approved apps can be launched without repeating per-run confirmation:

```powershell
python -m autopilot_ai.cli --approve-app notepad
python -m autopilot_ai.cli --list-approved-apps
```

After approving directories, pass only the filename:

```powershell
python -m autopilot_ai.cli "AutoPilot_AI_Project_Blueprint.pdf"
```

### Bounded file search, inspect, and copy loop

The current iterative workflow can search approved `Downloads` or a named approved folder for PDFs or text files, inspect each result for a literal phrase, and copy matching files into a subfolder of the primary workspace. It stops if the search or a read is incomplete or cannot be verified. Copies and a newly created destination folder require an explicit `--approve` run:

```powershell
python -m autopilot_ai.cli "Find all PDFs in Downloads, check which ones mention 'invoice', and copy them to workspace/invoices" --planner heuristic
python -m autopilot_ai.cli "Find all PDFs in Downloads, check which ones mention 'invoice', and copy them to workspace/invoices" --planner heuristic --approve
```

`Downloads` must already be an approved directory, and the search is limited to 100 files. Phrase matching is case-insensitive literal text matching; this workflow does not infer semantic similarity or inspect image-only/scanned PDF pages with OCR. It is a bounded file workflow, not yet a general-purpose dynamic tool-selection loop.

To list entries:

```powershell
python -m autopilot_ai.cli "Show me the files in this folder"
```

To search filenames recursively:

```powershell
python -m autopilot_ai.cli "Find files containing budget"
```

To read an existing file, use a relative path under the selected workspace or an approved directory. A bare filename is looked up across approved roots, including nested folders, when it identifies one unique file. If multiple files share the name, specify a relative path. Absolute paths work only when they remain inside an approved root. Text and supported code/data files are decoded as UTF-8 with replacement for invalid bytes; PDFs are read with `pypdf`. A PDF page can be supplied through a direct tool call; natural-language requests read the whole PDF. `Open analysis.pdf` and `Open file notes.md` mean read the supported file's contents; they do not start a GUI document viewer. Output is limited to 8,000 characters by default.

```powershell
python -m autopilot_ai.cli "Read file README.md" --planner heuristic --workspace D:\Agent
python -m autopilot_ai.cli "Show contents of file notes.txt" --planner heuristic
```

To find files by extension in the workspace and approved search folders:

```powershell
python -m autopilot_ai.cli "Find PDFs" --planner heuristic
python -m autopilot_ai.cli "Search for DOCX files in approved folders" --planner heuristic
python -m autopilot_ai.cli "Find images" --planner heuristic
```

You can scope a filename or extension search to a named folder inside an approved directory, for example `Show PDF files in ML folder`, `Show PDFs in ML`, or `Find files containing Renish_Resume in ML`. AutoPilot checks for a unique matching folder among the approved roots. If more than one matches, it asks you to repeat the request with the exact folder path. Without a named folder, searches scan all approved roots recursively. Normal output states the file type/name criteria and exact searched locations.

Extension search matches filename suffixes only; it does not inspect file contents. Image search currently includes PNG, JPG/JPEG, GIF, BMP, WEBP, TIF/TIFF, SVG, and HEIC files. Results stay within the configured approved roots.

Search results in normal CLI output are numbered and show both each filename and its full path. Start `python -m autopilot_ai.cli --interactive --planner heuristic` for a persistent prompt session. Its session ID is printed at startup; use `--resume SESSION_ID` with `--interactive` to continue recording in that session. Each one-shot command also starts or can resume a session and prints its ID in normal output. Task history is stored locally in `data/autopilot_memory.db`; `--history` lists recent actions across sessions, while `--history SESSION_ID` lists one session. `TaskMemory.search_memory(query)` is available to search saved goals, tool names, statuses, and summaries. If multiple approved folders match a folder-scoped request, reply with a number or ordinal such as `the second one`; AutoPilot reruns the read-only search in that folder. After a search with multiple file matches, the same kind of reply identifies a path but does not open or change the file. Numbered-choice context remains temporary and is cleared when the process exits.

```powershell
python -m autopilot_ai.cli --interactive --planner heuristic
python -m autopilot_ai.cli --interactive --planner heuristic --resume 7e6b...your-session-id
python -m autopilot_ai.cli --history
python -m autopilot_ai.cli --history 7e6b...your-session-id
```

Add `--debug` to interactive mode to print the complete JSON report, including plans, tool results, verification evidence, and candidate paths.

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

To run a safe inspection command in the approved workspace:

```powershell
python -m autopilot_ai.cli "Run command 'git status'" --planner heuristic
python -m autopilot_ai.cli "Run command 'python --version'" --planner heuristic
```

Other terminal commands require approval. Command output is capped and the process must exit with code 0 for verification to pass:

```powershell
python -m autopilot_ai.cli "Run command python --help" --planner heuristic --approve
```

To launch an installed application, first review the planned launch and then repeat with approval. A successful approval is persisted in `data/approved_apps.json` for that application name, so later launches do not require `--approve`. Built-in aliases include Notepad, Calculator, Chrome (including Google Chrome), Edge, Explorer, CMD, PowerShell, VS Code, and Paint. Chrome is also discovered through Windows App Paths and standard installation locations. The verifier waits briefly and checks Windows' process list for the returned PID or executable name.

```powershell
python -m autopilot_ai.cli "Launch Notepad" --planner heuristic
python -m autopilot_ai.cli "Launch Notepad" --planner heuristic --approve
```

File changes are previewed first. Review the `plan` in the output, then repeat the request with `--approve` to authorize it. Supported changes include creating one new directory, copying one file to a new destination, or writing one text file under an approved root. Copy sources may be relative to any approved root by setting `source_root_index` in a direct tool call; the natural-language CLI currently uses the primary root.

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

To create, overwrite, or append to a UTF-8 text file, include the file name and content in the request. New files use `create_new`; existing files are not overwritten unless you explicitly ask for `overwrite` or `append`. “Add” means append. If a create request names an existing file, AutoPilot asks you to rerun with an explicit mode. Review the target and mode, then rerun with `--approve`:

```powershell
python -m autopilot_ai.cli "Write file notes.txt with content First line" --planner heuristic
python -m autopilot_ai.cli "Write file notes.txt with content First line" --planner heuristic --approve
python -m autopilot_ai.cli "Append to file notes.txt with content`nSecond line" --planner heuristic --approve
python -m autopilot_ai.cli "Add 'Third line' to file notes.txt" --planner heuristic --approve
python -m autopilot_ai.cli "Overwrite file notes.txt with content Replacement text" --planner heuristic --approve
```

Writes are staged beside the destination and atomically published. A successful write is verified from disk using the resulting file's SHA-256 digest. Write operations are never automatically retried.

Desktop mouse and keyboard actions are available as one explicitly described action per command. Click coordinates must be inside the primary display, keys are validated against a bounded key set, PyAutoGUI's failsafe remains enabled, and every action requires `--approve`. Review the action and its inputs in the approval plan before rerunning with approval. Interactive mode cannot pre-approve these actions.

```powershell
python -m autopilot_ai.cli "Click at (500, 300)" --planner heuristic
python -m autopilot_ai.cli "Click at (500, 300)" --planner heuristic --approve
python -m autopilot_ai.cli 'Type text "Hello from AutoPilot"' --planner heuristic --approve
python -m autopilot_ai.cli "Press Ctrl+S" --planner heuristic --approve
python -m autopilot_ai.cli "Press Enter" --planner heuristic --approve
```

Typing and key presses go to whichever window is active when the approved command runs. Verification rechecks the primary screen, cursor position for mouse actions, and the foreground window. Desktop actions are never retried automatically.

The `python` commands assume the `.venv` created above is activated. Otherwise prefix each command with the virtual environment's Python executable so it uses the environment where Playwright was installed. `--approve-dir` adds folders to filename search and file reading; write operations still require `--approve`.

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

The default suite includes isolated filesystem tests and unit tests for planner, policy, retry and verification behavior. Most browser and desktop unit tests use mocked observations. `tests/test_integration.py` also contains a real filesystem workflow test; its local Chromium and live Windows UIA tests are opt-in. On this machine, the global pytest install has a broken optional `langsmith` plugin; set `$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"` before running pytest to avoid loading unrelated global plugins.

To run the real Playwright test against a temporary loopback-only HTTP page (with Chromium already installed under `D:\Agent\down`):

```powershell
$env:AUTOPILOT_RUN_BROWSER_INTEGRATION = "1"
python -m pytest tests/test_integration.py::test_real_playwright_follows_redirect_to_local_fixture
```

To run the real UI Automation check, use an unlocked interactive Windows desktop with a foreground app:

```powershell
$env:AUTOPILOT_RUN_WINDOWS_INTEGRATION = "1"
python -m pytest tests/test_integration.py::test_real_windows_uia_reads_foreground_window
```

The live UIA test only reads the foreground window. It skips when the test runner has no interactive window, as is common in service or sandbox sessions.

To require a live plan from the configured local Ollama model (the test fails if the deterministic fallback was used):

```powershell
$env:AUTOPILOT_RUN_OLLAMA_INTEGRATION = "1"
python -m pytest tests/test_integration.py::test_live_ollama_plan_is_used_only_when_it_matches_intent
```

## CLI results

By default, the CLI prints a short human-readable report with the task, status, actual result, verification and next action when needed. Use `--debug` to print the complete structured JSON report, including the plan and verification evidence:

```powershell
python -m autopilot_ai.cli "Find report.pdf"
python -m autopilot_ai.cli "Find report.pdf" --debug
```

Multi-step tasks report `Partially completed` if earlier steps were verified but a later step failed. A failed operation is not described as completed.

## Current improvement phase

The current work is auditing and hardening the existing search, file operations, browser navigation, terminal execution, application launch, desktop inspection, planning, verification, recovery and CLI behavior before adding capabilities. Calendar integration and additional desktop controls remain out of scope until the existing capabilities meet their acceptance criteria. The React dashboard is now available as described below. Do not expose unrestricted shell execution or claim task success without verification.

## Safety boundary

Registered tools are allowlisted and validate arguments with Pydantic. Search checks filename matches only, skips symlinks, reports inaccessible locations or limits as incomplete, and returns full paths. File reading accepts existing regular files under the selected workspace or approved directories, resolves a unique bare filename across those roots, rejects ambiguity, traversal, and symlinks, and independently rechecks the returned content slice, file size, and SHA-256 digest. Create-directory, copy, and write operations require `--approve`; writes are confined to approved roots, stage beside the destination, and atomically publish without automatic retries. Terminal commands run only in an approved directory; destructive commands are denied and all non-inspection commands require `--approve`. Browser access is read-only and rejects local/private destinations. Each application name requires one explicit `--approve` before it is persisted as trusted; launches use PATH, Windows App Paths, and limited standard-install discovery, then are verified by PID or executable name. Desktop inspection only reads accessibility information or captures a screenshot; desktop actions are separately allowlisted, require approval, and are checked against screen and foreground-window state. This phase does not implement deletion, calendar changes, or unrestricted command execution.

## Web Dashboard

Autopilot AI includes a web dashboard for interacting with the local agent.

### Quick Start

Build the frontend once, then launch the server:

```powershell
.\build.ps1
python -m autopilot_ai.web
```

Open http://localhost:8000 in your browser. The backend is bound to the local machine.

### Development Mode

For frontend development with hot reload, run the backend and frontend in separate terminals:

```powershell
# Terminal 1: backend
python -m autopilot_ai.web

# Terminal 2: frontend
Set-Location frontend
npm.cmd install
npm.cmd run dev
```

Open http://localhost:5173. Vite proxies API and WebSocket requests to the backend on port 8000.

### Features

- Task execution events over WebSocket
- Approval gates for filesystem write operations
- Session history persisted locally in SQLite
- Tool output and verification inspector
- Minimal grayscale interface with Inter and JetBrains Mono fonts

The dashboard displays results from existing agent capabilities. It does not add file opening, browser launching, desktop control, calendar operations, or unrestricted command execution.

### Example requests

Choose **Heuristic** for deterministic examples or **Ollama** to test local-model planning:

```text
Open https://python.org
Search for autopilot in workspace
Show files in workspace
Find .txt files in workspace
Inspect active window
Search the web for Python 3.12 release notes
What apps are approved?
What folders are approved?
```

These requests are read-only. Some websites may block automated browsing, and web search can be temporarily unavailable.

The following actions start an application or change files. Review the approval card before approving:

```text
Open Notepad
Create folder test-output
```

`Run dir command in workspace` is a read-only listing and does not require approval under the current command policy.
