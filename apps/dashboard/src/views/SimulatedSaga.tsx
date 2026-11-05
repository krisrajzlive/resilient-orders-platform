import { useMachine } from "@xstate/react";
import { useMemo, useRef, useState } from "react";
import { Badge } from "../components/Badge";
import { FaultSelectors, StepNode } from "../components/sagaShared";
import { orderSagaMachine, type LogEntry, type Step } from "../machines/orderSagaMachine";
import { sagaActors, type FailurePlans } from "../machines/sagaActors";
import { COMPENSATION, FORWARD, type NodeStatus } from "../machines/sagaProgress";

function statusOf(step: Step, value: unknown, log: LogEntry[]): NodeStatus {
  if (value === step) return "active";
  if (value === `${step}Backoff`) return "retrying";
  const last = [...log].reverse().find((l) => l.step === step);
  if (last?.status === "succeeded") return "done";
  if (last?.status === "failed") return "failed";
  return "waiting";
}

/** The same order lifecycle running entirely in the browser: no AWS needed. */
export function SimulatedSaga() {
  const [plans, setPlans] = useState<FailurePlans>({});
  const plansRef = useRef(plans);
  plansRef.current = plans;

  const machine = useMemo(
    () => orderSagaMachine.provide({ actors: sagaActors(() => plansRef.current, 700) }),
    [],
  );
  const [snap, send] = useMachine(machine);
  const value = snap.value;
  const log = snap.context.log;
  const running = value !== "idle" && !["confirmed", "rejected", "manualReview"].includes(String(value));
  const compensating = log.some((l) => l.status === "failed") || COMPENSATION.includes(String(value).replace("Backoff", "") as Step);
  const node = (s: Step) => (
    <StepNode key={s} label={s} status={statusOf(s, value, log)}
              failures={log.filter((l) => l.step === s && l.status === "retrying").length} />
  );

  return (
    <section className="stack">
      <div className="panel stack">
        <p className="muted">
          Runs in your browser as an XState machine; nothing is sent to AWS. Choose how each step behaves, place an
          order and watch retries, compensation and escalation.
        </p>
        <FaultSelectors plans={plans} onChange={setPlans} disabled={running} />
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
          <ol className="plain nodes">{FORWARD.map(node)}</ol>
        </div>
        <div className={`panel stack ${compensating ? "" : "dim"}`}>
          <strong>Compensation</strong>
          <ol className="plain nodes">{COMPENSATION.map(node)}</ol>
        </div>
      </div>

      {value === "confirmed" && <p className="banner banner-ok">Order {snap.context.orderId} confirmed.</p>}
      {value === "rejected" && <p className="banner banner-warn">Order {snap.context.orderId} rejected. Completed work was compensated.</p>}
      {value === "manualReview" && <p className="banner banner-bad">Compensation failed for {snap.context.orderId}. Escalated to manual review; an alert would be raised.</p>}

      {log.length > 0 && (
        <div className="panel stack">
          <strong>Execution history</strong>
          <ol className="plain log">
            {log.map((l, i) => (
              <li key={i}><span className="muted">{i + 1}.</span> {l.step} <Badge tone={l.status === "succeeded" ? "ok" : l.status === "failed" ? "bad" : "warn"}>{l.status}</Badge> <span className="muted">attempt {l.attempt}</span></li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
