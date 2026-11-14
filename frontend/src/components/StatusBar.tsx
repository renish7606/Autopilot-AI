import { useEffect, useState } from "react";
import { Circle } from "lucide-react";

interface StatusResponse {
  ollama_available?: boolean;
  workspace?: string;
}

interface StatusBarProps {
  sessionId: string | null;
  connected: boolean;
}

export default function StatusBar({ sessionId, connected }: StatusBarProps) {
  const [status, setStatus] = useState<StatusResponse | null>(null);

  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const response = await fetch("/api/status", { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error(`Status request failed (${response.status}).`);
        const body: unknown = await response.json();
        if (active && body && typeof body === "object") setStatus(body as StatusResponse);
      } catch {
        if (active) setStatus(null);
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-zinc-800 bg-zinc-950 px-5">
      <div className="flex min-w-0 items-center gap-5">
        <span className="shrink-0 text-sm font-semibold tracking-tight text-zinc-50">Autopilot AI</span>
        <span className="hidden min-w-0 items-center gap-2 text-xs text-zinc-500 sm:flex" title={status?.workspace ?? "Workspace unavailable"}>
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${status?.ollama_available ? "bg-green-500" : "bg-red-500"}`} />
          Ollama {status?.ollama_available ? "available" : "unavailable"}
          {status?.workspace && <span className="ml-2 max-w-[24rem] truncate font-mono">{status.workspace}</span>}
        </span>
      </div>
      <div className="flex items-center gap-3 text-xs text-zinc-500">
        <span className="inline-flex items-center gap-1.5" title={connected ? "Connected to agent" : "Disconnected from agent"}>
          <Circle size={8} fill="currentColor" className={connected ? "text-green-500" : "text-red-500"} />
          {connected ? "Connected" : "Disconnected"}
        </span>
        {sessionId && <span className="hidden font-mono sm:inline" title={sessionId}>Session {sessionId.slice(0, 8)}</span>}
      </div>
    </header>
  );
}
