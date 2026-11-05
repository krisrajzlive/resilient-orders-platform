import { useMachine } from "@xstate/react";
import { useMemo, useRef, useState } from "react";
import { Badge, type Tone } from "../components/Badge";
import {
  orderSagaMachine, type LogEntry, type Step,
} from "../machines/orderSagaMachine";
import { sagaActors, type FailurePlan, type FailurePlans } from "../machines/sagaActors";

const FORWARD: Step[] = ["ReserveStock", "ChargePayment", "CreateShipment"];
const COMPENSATION: Step[] = ["RefundPayment", "ReleaseStock"];
const PLANS: FailurePlan[] = ["none", "transient", "permanent"];

type StepStatus = "waiting" | "active" | "retrying" | "done" | "failed";

function statusOf(step: Step, value: unknown, log: LogEntry[]): StepStatus {
  if (value === step) return "active";
  if (value === `${step}Backoff`) return "retrying";
  const last = [...log].reverse().find((l) => l.step === step);
  if (last?.status === "succeeded") return "done";
  if (last?.status === "failed") return "failed";
  return "waiting";
}

const TONE: Record<StepStatus, Tone> = { waiting: "muted", active: "info", retrying: "warn", done: "ok", failed: "bad" };

function StepNode({ step, value, log }: { step: Step; value: unknown; log: LogEntry[] }) {
  const status = statusOf(step, value, log);
  const attempts = log.filter((l) => l.step === step && l.status === "retrying").length;
  return (
    <li className={`node node-${status}`}>
      <span>{step}</span>
      <Badge tone={TONE[status]}>{status}{attempts > 0 ? ` · ${attempts} retr${attempts > 1 ? "ies" : "y"}` : ""}</Badge>
    </li>
  );
}

export function OrderSagaView() {
  const [plans, setPlans] = useState<FailurePlans>({});
  const plansRef = useRef(plans);
  plansRef.current = plans;

  const machine = useMemo(
    () => orderSagaMachine.provide({ actors: sagaActors(() => plansRef.current, 700) }),
    [],
  );
  const [snap, send] = useMachine(machine);
  const value = snap.value;
  const running = value !== "idle" && !["confirmed", "rejected", "manualReview"].includes(String(value));
  const compensating = snap.context.log.some((l) => l.status === "failed") || COMPENSATION.includes(String(value).replace("Backoff", "") as Step);

  return (
    <section className="stack">
      <div className="panel stack">
        <p className="muted">
          Order lifecycle as an XState machine. Choose how each step behaves, place an order and watch retries,
          compensation and escalation. The state names match the Step Functions workflow.
        </p>
        <div className="plans">
          {[...FORWARD, ...COMPENSATION].map((s) => (
            <label key={s} className="plan">
              <span>{s}</span>
              <select value={plans[s] ?? "none"} disabled={running}
                      onChange={(e) => setPlans({ ...plans, [s]: e.target.value as FailurePlan })}>
                {PLANS.map((p) => <option key={p} value={p}>{p === "none" ? "works" : p === "transient" ? "fails twice, then works" : "always fails"}</option>)}
              </select>
            </label>
          ))}
        </div>
        <div className="row">
          <button disabled={running} onClick={() => send({ type: "PLACE_ORDER", orderId: `ord-${Date.now().toString(36)}` })}>
            Place order
          </button>
          <button className="secondary" disabled={value === "idle"} onClick={() => send({ type: "RESET" })}>Reset</button>
          <Badge tone="muted">{String(value)}</Badge>
        </div>
      </div>

      <div className="grid2">
        <div className="panel stack">
          <strong>Forward steps</strong>
          <ol className="plain nodes">{FORWARD.map((s) => <StepNode key={s} step={s} value={value} log={snap.context.log} />)}</ol>
        </div>
        <div className={`panel stack ${compensating ? "" : "dim"}`}>
          <strong>Compensation</strong>
          <ol className="plain nodes">{COMPENSATION.map((s) => <StepNode key={s} step={s} value={value} log={snap.context.log} />)}</ol>
        </div>
      </div>

      {value === "confirmed" && <p className="banner banner-ok">Order {snap.context.orderId} confirmed.</p>}
      {value === "rejected" && <p className="banner banner-warn">Order {snap.context.orderId} rejected. Completed work was compensated.</p>}
      {value === "manualReview" && <p className="banner banner-bad">Compensation failed for {snap.context.orderId}. Escalated to manual review; an alert would be raised.</p>}

      {snap.context.log.length > 0 && (
        <div className="panel stack">
          <strong>Execution history</strong>
          <ol className="plain log">
            {snap.context.log.map((l, i) => (
              <li key={i}><span className="muted">{i + 1}.</span> {l.step} <Badge tone={l.status === "succeeded" ? "ok" : l.status === "failed" ? "bad" : "warn"}>{l.status}</Badge> <span className="muted">attempt {l.attempt}</span></li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
