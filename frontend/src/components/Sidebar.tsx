import { useCallback, useEffect, useState } from "react";
import { Clock3, Folder, Plus, RefreshCw } from "lucide-react";
import type { HistoryEntry, SessionInfo } from "../lib/types";

interface SidebarProps {
  activeSessionId: string | null;
  onNewSession: () => void;
  onSelectSession: (sessionId: string, history: HistoryEntry[]) => void;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`Request failed (${response.status}).`);
  return response.json() as Promise<T>;
}

export default function Sidebar({ activeSessionId, onNewSession, onSelectSession }: SidebarProps) {
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [directories, setDirectories] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadingSession, setLoadingSession] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [sessionData, directoryData] = await Promise.all([
        getJson<{ sessions: SessionInfo[] }>("/api/sessions"),
        getJson<{ approved_dirs: string[] }>("/api/approved-dirs"),
      ]);
      setSessions(Array.isArray(sessionData.sessions) ? sessionData.sessions : []);
      setDirectories(Array.isArray(directoryData.approved_dirs) ? directoryData.approved_dirs : []);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load sidebar data.");
    }
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  const selectSession = async (sessionId: string) => {
    setLoadingSession(sessionId);
    setError(null);
    try {
      const data = await getJson<{ history: HistoryEntry[] }>(`/api/sessions/${encodeURIComponent(sessionId)}/history`);
      onSelectSession(sessionId, Array.isArray(data.history) ? data.history : []);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not load session history.");
    } finally {
      setLoadingSession(null);
    }
  };

  return (
    <aside className="flex min-h-0 w-64 shrink-0 flex-col border-r border-zinc-800 bg-zinc-950">
      <div className="flex items-center justify-between px-4 pb-3 pt-4">
        <h2 className="text-xs font-medium uppercase tracking-wider text-zinc-500">Sessions</h2>
        <button type="button" onClick={() => void refresh()} className="rounded p-1 text-zinc-500 hover:bg-zinc-800 hover:text-white" aria-label="Refresh sessions and approved folders" title="Refresh">
          <RefreshCw size={14} />
        </button>
      </div>
      <div className="px-3">
        <button type="button" onClick={onNewSession} className="flex w-full items-center justify-center gap-2 rounded-md border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800">
          <Plus size={15} /> New Session
        </button>
      </div>
      {error && <p role="alert" className="mx-4 mt-3 text-xs text-red-400">{error}</p>}
      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-5">
        <div className="mt-4 space-y-1">
          {sessions.length === 0 && <p className="px-2 py-2 text-xs text-zinc-600">No saved sessions yet.</p>}
          {sessions.map((session) => (
            <button key={session.session_id} type="button" disabled={loadingSession !== null} onClick={() => void selectSession(session.session_id)}
              className={`w-full rounded-md px-2.5 py-2 text-left transition-colors hover:bg-zinc-900 disabled:opacity-60 ${activeSessionId === session.session_id ? "bg-zinc-800" : ""}`}>
              <span className="block truncate text-xs text-zinc-200" title={session.last_goal}>{session.last_goal || "Untitled task"}</span>
              <span className="mt-1 flex items-center gap-1.5 text-[11px] text-zinc-600">
                <Clock3 size={11} /> {session.task_count} {session.task_count === 1 ? "task" : "tasks"}
                {loadingSession === session.session_id && <span className="ml-auto">Loading…</span>}
              </span>
            </button>
          ))}
        </div>
        <div className="mt-7">
          <h2 className="mb-2 flex items-center gap-2 px-2 text-xs font-medium uppercase tracking-wider text-zinc-500"><Folder size={13} /> Approved Roots</h2>
          {directories.length === 0 && <p className="px-2 py-2 text-xs text-zinc-600">No approved folders.</p>}
          <ul className="space-y-1">
            {directories.map((directory) => <li key={directory} className="truncate rounded px-2 py-1 font-mono text-[11px] text-zinc-500" title={directory}>{directory}</li>)}
          </ul>
        </div>
      </div>
    </aside>
  );
}
