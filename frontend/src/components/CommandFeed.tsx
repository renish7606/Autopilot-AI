import { useEffect, useRef, useState } from "react";
import { Bot, LoaderCircle, UserRound } from "lucide-react";
import type { AgentEvent, HistoryEntry, RunOptions } from "../lib/types";
import ApprovalCard from "./ApprovalCard";
import InputBar from "./InputBar";
import StepCard from "./StepCard";

interface CommandFeedProps {
  events: AgentEvent[];
  history: HistoryEntry[];
  isRunning: boolean;
  isConnected: boolean;
  activeSessionId: string | null;
  pendingApproval: AgentEvent | null;
  onSubmit: (goal: string, options: RunOptions) => void;
  onApprove: (goal: string, sessionId: string) => void;
  onDeny: () => void;
}

function prettyStatus(status: string): string {
  return status.replace(/_/g, " ").replace(/^./, (character: string) => character.toUpperCase());
}

export default function CommandFeed({ events, history, isRunning, isConnected, activeSessionId, pendingApproval, onSubmit, onApprove, onDeny }: CommandFeedProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const [deniedApproval, setDeniedApproval] = useState<AgentEvent | null>(null);

  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" }); }, [events, history, isRunning, deniedApproval]);

  useEffect(() => { setDeniedApproval(null); }, [activeSessionId]);

  return (
    <section className="flex min-h-0 min-w-0 flex-1 flex-col bg-zinc-950">
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto px-5 py-6">
        <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-3">
          {events.length === 0 && history.length === 0 && !isRunning && <div className="m-auto max-w-md text-center">
            <div className="mx-auto mb-4 flex h-10 w-10 items-center justify-center rounded-lg border border-zinc-800 bg-zinc-900"><Bot size={19} className="text-zinc-400" /></div>
            <h1 className="text-base font-medium text-zinc-200">What can I help you do?</h1>
            <p className="mt-2 text-sm leading-6 text-zinc-500">Give the local agent a task. It will select an approved tool, apply safety checks, and report what it could verify.</p>
          </div>}

          {history.length > 0 && events.length === 0 && <>
            <h2 className="mb-1 text-xs font-medium uppercase tracking-wider text-zinc-600">Session history</h2>
            {history.map((entry) => <article key={entry.id} className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0"><p className="text-sm text-zinc-200">{entry.goal}</p><p className="mt-2 text-xs text-zinc-500">{entry.summary}</p></div>
                <span className={`shrink-0 rounded border border-zinc-700 px-2 py-0.5 text-[10px] ${entry.status === "success" || entry.status === "completed" ? "text-green-500" : entry.status === "error" || entry.status === "failed" ? "text-red-400" : "text-zinc-400"}`}>{prettyStatus(entry.status)}</span>
              </div>
              <p className="mt-3 font-mono text-[10px] text-zinc-600">{entry.tool_name} · {new Date(entry.timestamp).toLocaleString()}</p>
            </article>)}
          </>}

          {events.map((event, index) => {
            switch (event.event) {
              case "task_started":
                return <article key={`${index}-goal`} className="ml-auto max-w-[85%] rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-3">
                  <span className="mb-2 flex items-center gap-1.5 text-[11px] text-zinc-500"><UserRound size={12} /> You</span>
                  <p className="whitespace-pre-wrap text-sm leading-6 text-zinc-200">{event.goal}</p>
                </article>;
              case "planning":
                return <div key={`${index}-planning`} className="flex items-center gap-2 px-1 py-2 text-xs text-zinc-500"><LoaderCircle size={14} className="animate-spin" /> Planning with {event.planner ?? "agent"}…</div>;
              case "step_done":
                return <StepCard key={`${index}-step`} event={event} />;
              case "approval_required":
                return deniedApproval === event ? <article key={`${index}-denied`} className="rounded-lg border border-zinc-800 bg-zinc-900 p-4 text-sm text-zinc-400">Approval declined. The pending operation was not run.</article> : pendingApproval === event ? <ApprovalCard key={`${index}-approval`} event={event} onApprove={() => {
                  const goal = event.goal;
                  const sessionId = event.session_id ?? activeSessionId;
                  if (goal && sessionId) onApprove(goal, sessionId);
                }} onDeny={() => { setDeniedApproval(event); onDeny(); }} /> : null;
              case "clarification":
                return <article key={`${index}-clarify`} className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
                  <h3 className="text-sm font-medium text-zinc-200">More information needed</h3>
                  <p className="mt-2 text-sm text-zinc-400">{event.question || event.message || "Please clarify the request."}</p>
                  {Array.isArray(event.candidates) && event.candidates.length > 0 && <ol className="mt-3 list-inside list-decimal space-y-1 text-xs text-zinc-500">{event.candidates.map((candidate, candidateIndex) => <li key={candidateIndex}>{typeof candidate === "string" ? candidate : JSON.stringify(candidate)}</li>)}</ol>}
                </article>;
              case "task_completed":
              case "task_failed": {
                const report = event.report;
                const success = event.event === "task_completed";
                return <article key={`${index}-final`} className="rounded-lg border border-zinc-800 bg-zinc-900 p-4">
                  <div className="flex items-center gap-2"><span className={`h-2 w-2 rounded-full ${success ? "bg-green-500" : "bg-red-500"}`} /><h3 className="text-sm font-medium text-zinc-200">{success ? "Task finished" : "Task failed"}</h3></div>
                  {report?.summary && <p className="mt-2 text-sm leading-6 text-zinc-400">{report.summary}</p>}
                  {report?.message && <p className="mt-2 text-sm leading-6 text-zinc-400">{report.message}</p>}
                  {!report?.summary && !report?.message && <p className="mt-2 text-xs text-zinc-500">{report?.status ? prettyStatus(report.status) : success ? "Completed" : "Failed"}</p>}
                  {report?.planner_note && <p className="mt-2 text-xs text-zinc-600">{report.planner_note}</p>}
                </article>;
              }
              case "error":
                return <article role="alert" key={`${index}-error`} className="rounded-lg border border-red-900/60 bg-zinc-900 p-4 text-sm text-red-300">{event.message ?? "An unexpected server error occurred."}</article>;
              default:
                return null;
            }
          })}
          {isRunning && !events.some((event) => event.event === "planning") && <div className="flex items-center gap-2 px-1 py-2 text-xs text-zinc-500"><LoaderCircle size={14} className="animate-spin" /> Running…</div>}
          <div ref={endRef} />
        </div>
      </div>
      <InputBar isRunning={isRunning} isConnected={isConnected} onSubmit={onSubmit} />
    </section>
  );
}
