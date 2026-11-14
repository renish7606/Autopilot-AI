import { Check, ChevronDown, ChevronRight, CircleX } from "lucide-react";
import { useState } from "react";
import type { AgentEvent } from "../lib/types";

interface StepCardProps { event: AgentEvent; }

export default function StepCard({ event }: StepCardProps) {
  const [expanded, setExpanded] = useState(false);
  const verified = event.verification?.passed === true;
  const failed = event.verification?.passed === false || event.tool_result?.status === "error" || event.tool_result?.status === "denied";
  const statusLabel = verified ? "Verified" : failed ? "Failed" : "Unverified";
  const output = event.tool_result?.output;

  return (
    <article className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="rounded border border-zinc-700 bg-zinc-950 px-2 py-0.5 font-mono text-[11px] text-zinc-300">{event.plan?.tool_name ?? "tool"}</span>
          {typeof event.attempts === "number" && event.attempts > 1 && <span className="text-[11px] text-zinc-600">{event.attempts} attempts</span>}
        </div>
        <span className={`inline-flex items-center gap-1.5 text-xs ${verified ? "text-green-500" : failed ? "text-red-400" : "text-zinc-500"}`}>
          {verified ? <Check size={13} /> : failed ? <CircleX size={13} /> : <span className="h-2 w-2 rounded-full bg-zinc-600" />}
          {statusLabel}
        </span>
      </div>
      <p className="mt-3 text-sm leading-5 text-zinc-200">{event.plan?.description ?? "Execution step"}</p>
      {event.verification?.summary && <p className="mt-2 text-xs leading-5 text-zinc-500">{event.verification.summary}</p>}
      {output !== undefined && (
        <div className="mt-3 border-t border-zinc-800 pt-2">
          <button type="button" onClick={() => setExpanded((value) => !value)} className="flex items-center gap-1 text-xs text-zinc-500 hover:text-zinc-200" aria-expanded={expanded}>
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Tool output
          </button>
          {expanded && <pre className="mt-2 max-h-64 overflow-auto rounded bg-zinc-950 p-3 font-mono text-[11px] leading-5 text-zinc-400">{JSON.stringify(output, null, 2)}</pre>}
        </div>
      )}
    </article>
  );
}
