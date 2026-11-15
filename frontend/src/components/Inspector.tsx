import { useState } from "react";
import { Check, CircleX } from "lucide-react";
import type { AgentEvent, HistoryEntry } from "../lib/types";

interface InspectorProps {
  events: AgentEvent[];
  history: HistoryEntry[];
}

type Tab = "terminal" | "verifier";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function outputFor(event: AgentEvent | undefined): unknown {
  const output = event?.tool_result?.output;
  if (isRecord(output) && "output" in output) return output.output;
  return output;
}

function historyToolName(entry: HistoryEntry | undefined): string | undefined {
  if (isRecord(entry?.output) && isRecord(entry.output.plan) && typeof entry.output.plan.tool_name === "string") {
    return entry.output.plan.tool_name;
  }
  return entry?.tool_name;
}

function historyToolOutput(entry: HistoryEntry | undefined): unknown {
  if (isRecord(entry?.output) && isRecord(entry.output.tool_result)) {
    return entry.output.tool_result.output;
  }
  return entry?.output;
}

export default function Inspector({ events, history }: InspectorProps) {
  const [tab, setTab] = useState<Tab>("terminal");
  const steps = events.filter((event) => event.event === "step_done");
  const latest = steps[steps.length - 1];
  const latestHistory = history[history.length - 1];
  const verification = latest?.verification;
  const toolName = latest?.plan?.tool_name ?? historyToolName(latestHistory);
  const output = latest ? outputFor(latest) : historyToolOutput(latestHistory);
  const commandOutput = isRecord(output) ? output : null;

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
            {toolName === "run_command" && commandOutput ? <div className="space-y-3 rounded-md bg-zinc-900 p-3">
              <div className="flex items-center gap-2 text-xs text-zinc-500">
                <span>exit code:</span>
                <code className={`font-mono ${commandOutput.exit_code === 0 ? "text-green-500" : "text-red-400"}`}>{String(commandOutput.exit_code ?? "?")}</code>
                {commandOutput.timed_out === true && <span className="text-amber-500">timed out</span>}
              </div>
              {typeof commandOutput.stdout === "string" && commandOutput.stdout.length > 0 && <div>
                <p className="mb-1 text-[10px] uppercase tracking-wider text-zinc-600">stdout</p>
                <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-zinc-300">{commandOutput.stdout}</pre>
              </div>}
              {typeof commandOutput.stderr === "string" && commandOutput.stderr.length > 0 && <div>
                <p className="mb-1 text-[10px] uppercase tracking-wider text-zinc-600">stderr</p>
                <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-red-400">{commandOutput.stderr}</pre>
              </div>}
              {typeof commandOutput.cwd === "string" && <p className="font-mono text-[10px] text-zinc-600">cwd: {commandOutput.cwd}</p>}
            </div> : <pre className="min-h-24 rounded-md bg-zinc-900 p-3 font-mono text-[11px] leading-5 text-zinc-400">{JSON.stringify(output ?? "No tool output yet.", null, 2)}</pre>}
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
