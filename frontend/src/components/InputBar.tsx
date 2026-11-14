import { ArrowUp, LoaderCircle } from "lucide-react";
import { useState, type FormEvent } from "react";
import type { RunOptions } from "../lib/types";

interface InputBarProps {
  isRunning: boolean;
  isConnected: boolean;
  onSubmit: (goal: string, options: RunOptions) => void;
}

const QUICK_ACTIONS = [
  "List files in workspace",
  "Find .pdf files in workspace",
  "Open https://example.com",
  "Run dir command in workspace",
  "Open Notepad",
];

export default function InputBar({ isRunning, isConnected, onSubmit }: InputBarProps) {
  const [goal, setGoal] = useState("");
  const [planner, setPlanner] = useState<RunOptions["planner"]>("ollama");

  const submit = (event?: FormEvent) => {
    event?.preventDefault();
    const value = goal.trim();
    if (!value || isRunning || !isConnected) return;
    onSubmit(value, { planner });
    setGoal("");
  };

  return (
    <div className="shrink-0 border-t border-zinc-800 bg-zinc-950 px-5 pb-4 pt-3">
      {!isRunning && <div className="mb-3 flex flex-wrap gap-2">
        {QUICK_ACTIONS.map((action) => <button key={action} type="button" disabled={!isConnected} onClick={() => { onSubmit(action, { planner }); setGoal(""); }} className="rounded-full border border-zinc-800 bg-zinc-900 px-3 py-1 text-xs text-zinc-400 hover:border-zinc-600 hover:text-white disabled:cursor-not-allowed disabled:opacity-50">{action}</button>)}
      </div>}
      <form onSubmit={submit} className="flex items-end gap-2 rounded-lg border border-zinc-800 bg-zinc-900 p-2 focus-within:border-zinc-600">
        <div className="min-w-0 flex-1">
          <label htmlFor="goal-input" className="sr-only">What should I do?</label>
          <textarea id="goal-input" rows={1} value={goal} onChange={(event) => setGoal(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submit(); } }} disabled={isRunning} maxLength={2000} placeholder={isRunning ? "Agent is working…" : "What should I do?"} className="max-h-32 min-h-9 w-full resize-y bg-transparent px-2 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none disabled:opacity-60" />
        </div>
        <select aria-label="Planner" value={planner} onChange={(event) => setPlanner(event.target.value as RunOptions["planner"])} disabled={isRunning} className="mb-1 rounded border border-zinc-800 bg-zinc-950 px-2 py-1.5 text-[11px] text-zinc-500 disabled:opacity-50">
          <option value="ollama">Ollama</option><option value="heuristic">Heuristic</option>
        </select>
        <button type="submit" disabled={!goal.trim() || isRunning || !isConnected} aria-label={isRunning ? "Agent is running" : "Send goal"} className="mb-1 flex h-8 w-8 items-center justify-center rounded-md bg-white text-zinc-950 hover:bg-zinc-200 disabled:cursor-not-allowed disabled:bg-zinc-800 disabled:text-zinc-600">
          {isRunning ? <LoaderCircle size={16} className="animate-spin" /> : <ArrowUp size={17} />}
        </button>
      </form>
      <p className="mt-2 text-center text-[10px] text-zinc-600">Enter to send · Shift+Enter for a new line · {isRunning ? "Running…" : isConnected ? "Local agent" : "Waiting for server"}</p>
    </div>
  );
}
