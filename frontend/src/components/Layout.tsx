import type { AgentEvent, HistoryEntry } from "../lib/types";
import CommandFeed from "./CommandFeed";
import Inspector from "./Inspector";
import Sidebar from "./Sidebar";
import StatusBar from "./StatusBar";

interface LayoutProps {
  activeSessionId: string | null;
  connected: boolean;
  events: AgentEvent[];
  history: HistoryEntry[];
  isRunning: boolean;
  pendingApproval: AgentEvent | null;
  refreshKey: number;
  onNewSession: () => void;
  onSelectSession: (sessionId: string, history: HistoryEntry[]) => void;
  onSubmit: (goal: string, options: { planner?: "ollama" | "heuristic" }) => void;
  onApprove: (goal: string, sessionId: string) => void;
  onDeny: () => void;
}

export default function Layout(props: LayoutProps) {
  return (
    <main className="flex h-dvh min-h-[480px] flex-col overflow-hidden bg-zinc-950 text-zinc-50">
      <StatusBar sessionId={props.activeSessionId} connected={props.connected} />
      <div className="flex min-h-0 flex-1">
        <Sidebar key={props.refreshKey} activeSessionId={props.activeSessionId} onNewSession={props.onNewSession} onSelectSession={props.onSelectSession} />
        <CommandFeed events={props.events} history={props.history} isRunning={props.isRunning} isConnected={props.connected} activeSessionId={props.activeSessionId} pendingApproval={props.pendingApproval} onSubmit={props.onSubmit} onApprove={props.onApprove} onDeny={props.onDeny} />
        <Inspector events={props.events} history={props.history} />
      </div>
    </main>
  );
}
