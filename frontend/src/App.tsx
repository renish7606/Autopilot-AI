import { useEffect, useState } from "react";
import Layout from "./components/Layout";
import { useAgent } from "./hooks/useAgent";
import type { HistoryEntry, RunOptions } from "./lib/types";

export default function App() {
  const agent = useAgent();
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    if (agent.events.some((event) => event.event === "task_completed" || event.event === "task_failed")) {
      setRefreshKey((key) => key + 1);
    }
  }, [agent.events]);

  const newSession = () => {
    agent.newSession();
    setHistory([]);
  };

  const selectSession = (sessionId: string, entries: HistoryEntry[]) => {
    agent.selectSession(sessionId);
    setHistory(entries);
  };

  const submit = (goal: string, options: RunOptions) => {
    setHistory([]);
    agent.sendGoal(goal, options);
  };

  return <Layout
    activeSessionId={agent.currentSessionId}
    connected={agent.isConnected}
    events={agent.events}
    history={history}
    isRunning={agent.isRunning}
    pendingApproval={agent.pendingApproval}
    refreshKey={refreshKey}
    onNewSession={newSession}
    onSelectSession={selectSession}
    onSubmit={submit}
    onApprove={agent.sendApproval}
    onDeny={agent.dismissApproval}
  />;
}
