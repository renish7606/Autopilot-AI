import { useState } from "react";
import { Check, CircleX } from "lucide-react";
import type { AgentEvent, HistoryEntry } from "../lib/types";

interface InspectorProps {
  events: AgentEvent[];
  history: HistoryEntry[];
}

type Tab = "terminal" | "verifier";

function outputFor(event: AgentEvent | undefined): unknown {
  const output = event?.tool_result?.output;
  if (output && typeof output === "object" && "output" in output) return (output as { output: unknown }).output;
  return output;
}

export default function Inspector({ events, history }: InspectorProps) {
  const [tab, setTab] = useState<Tab>("terminal");
  const steps = events.filter((event) => event.event === "step_done");
  const latest = steps[steps.length - 1];
  const latestHistory = history[history.length - 1];
  const verification = latest?.verification;

  return (
    <aside className="flex min-h-0 w-80 shrink-0 flex-col border-l border-zinc-800 bg-zinc-950">
      <div role="tablist" aria-label="Execution details" className="flex h-11 shrink-0 border-b border-zinc-800 px-4">
        {(["terminal", "verifier"] as const).map((item) => <button key={item} type="button" role="tab" aria-selected={tab === item} onClick={() => setTab(item)} className={`mr-5 border-b-2 px-1 text-xs capitalize ${tab === item ? "border-white text-white" : "border-transparent text-zinc-500 hover:text-white"}`}>{item}</button>)}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {tab === "terminal" ? (
          <div>
            <div className="mb-3 flex items-center justify-between gap-2">
              <h2 className="text-xs font-medium text-zinc-300">Latest tool output</h2>
              <span className="truncate font-mono text-[10px] text-zinc-600">{latest?.plan?.tool_name ?? latestHistory?.tool_name ?? "—"}</span>
            </div>
            <pre className="min-h-24 rounded-md bg-zinc-900 p-3 font-mono text-[11px] leading-5 text-zinc-400">{JSON.stringify(latest ? outputFor(latest) : latestHistory?.output ?? "No tool output yet.", null, 2)}</pre>
            {latest?.tool_result?.error_code && <p className="mt-3 text-xs text-red-400">Error code: {latest.tool_result.error_code}</p>}
          </div>
        ) : (
          <div>
            <h2 className="mb-3 text-xs font-medium text-zinc-300">Verification</h2>
            {verification ? <>
              <p className={`flex items-center gap-2 text-sm ${verification.passed ? "text-green-500" : "text-red-400"}`}>
                {verification.passed ? <Check size={15} /> : <CircleX size={15} />}{verification.passed ? "Passed" : "Failed"}
              </p>
              <p className="mt-2 text-xs leading-5 text-zinc-400">{verification.summary}</p>
              <h3 className="mb-2 mt-5 text-[11px] font-medium uppercase tracking-wide text-zinc-600">Evidence</h3>
              <pre className="rounded-md bg-zinc-900 p-3 font-mono text-[11px] leading-5 text-zinc-400">{JSON.stringify(verification.evidence, null, 2)}</pre>
            </> : <p className="text-xs leading-5 text-zinc-600">No verification details for the selected session yet.</p>}
          </div>
        )}
      </div>
    </aside>
  );
}
