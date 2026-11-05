import { Badge, type Tone } from "./Badge";
import type { Step } from "../machines/orderSagaMachine";
import { COMPENSATION, FORWARD, type NodeStatus } from "../machines/sagaProgress";
import type { FailurePlan, FailurePlans } from "../machines/sagaActors";

export const TONE: Record<NodeStatus, Tone> = { waiting: "muted", active: "info", retrying: "warn", done: "ok", failed: "bad" };

export const FAULT_LABEL: Record<FailurePlan, string> = {
  none: "works",
  transient: "fails twice, then works",
  permanent: "always fails",
};

export function StepNode({ label, status, failures }: { label: string; status: NodeStatus; failures: number }) {
  return (
    <li className={`node node-${status}`}>
      <span>{label}</span>
      <Badge tone={TONE[status]}>
        {status}{failures > 0 ? ` · ${failures} failed attempt${failures > 1 ? "s" : ""}` : ""}
      </Badge>
    </li>
  );
}

/** One dropdown per step: how that step should behave when an order runs. */
export function FaultSelectors({ plans, onChange, disabled }: {
  plans: FailurePlans; onChange: (next: FailurePlans) => void; disabled: boolean;
}) {
  return (
    <div className="plans">
      {([...FORWARD, ...COMPENSATION] as Step[]).map((s) => (
        <label key={s} className="plan">
          <span>{s}</span>
          <select value={plans[s] ?? "none"} disabled={disabled}
                  onChange={(e) => onChange({ ...plans, [s]: e.target.value as FailurePlan })}>
            {(Object.keys(FAULT_LABEL) as FailurePlan[]).map((p) => <option key={p} value={p}>{FAULT_LABEL[p]}</option>)}
          </select>
        </label>
      ))}
    </div>
  );
}
