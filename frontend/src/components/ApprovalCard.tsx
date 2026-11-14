import { ShieldAlert } from "lucide-react";
import type { AgentEvent } from "../lib/types";

interface ApprovalCardProps {
  event: AgentEvent;
  onApprove: () => void;
  onDeny: () => void;
}

export default function ApprovalCard({ event, onApprove, onDeny }: ApprovalCardProps) {
  const plan = event.plan;
  const actions = Array.isArray(event.planned_actions) ? event.planned_actions : [];
  return (
    <article className="rounded-lg border border-amber-500/40 border-l-2 border-l-amber-500 bg-zinc-900 p-4">
      <div className="flex items-start gap-3">
        <ShieldAlert size={17} className="mt-0.5 shrink-0 text-amber-500" />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-medium text-zinc-100">Approval required</h3>
          <p className="mt-2 text-sm text-zinc-300">{plan?.description ?? (actions.length ? `${actions.length} planned action(s) will modify data.` : "This operation will change data.")}</p>
          {plan?.risk_level && <p className="mt-2 text-xs text-amber-500">Risk: {plan.risk_level}</p>}
          {event.message && <p className="mt-2 text-xs leading-5 text-zinc-500">{event.message}</p>}
          <div className="mt-4 flex gap-2">
            <button type="button" onClick={onApprove} className="rounded-md bg-white px-3 py-1.5 text-xs font-medium text-zinc-950 hover:bg-zinc-200">Approve</button>
            <button type="button" onClick={onDeny} className="rounded-md border border-zinc-700 px-3 py-1.5 text-xs text-zinc-300 hover:bg-zinc-800">Deny</button>
          </div>
          <p className="mt-3 text-[11px] text-zinc-600">Approving authorizes this pending operation for this request only.</p>
        </div>
      </div>
    </article>
  );
}
