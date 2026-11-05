import { useMachine } from "@xstate/react";
import { useMemo, useState } from "react";
import { fromPromise } from "xstate";
import { Badge } from "../components/Badge";
import { FaultSelectors, StepNode } from "../components/sagaShared";
import type { Step } from "../machines/orderSagaMachine";
import { remoteSagaMachine, type StartRequest } from "../machines/remoteSagaMachine";
import type { FailurePlans } from "../machines/sagaActors";
import { COMPENSATION, FORWARD, compensationStarted, stepStatus } from "../machines/sagaProgress";
import { catalogApi, type SagaFaults } from "../store/catalogApi";
import { toError } from "../store/errors";
import { useAppStore } from "../store/hooks";

const PRESETS = [
  { label: "Normal order", amount: 120 },
  { label: "High-value order (needs review)", amount: 9000 },
];

/** Only steps set to fail are sent; "works" is the default and is left out. */
const toFaults = (plans: FailurePlans): SagaFaults => {
  const faults: SagaFaults = {};
  for (const [step, plan] of Object.entries(plans)) {
    if (plan === "transient" || plan === "permanent") faults[step] = plan;
  }
  return faults;
};

/** The order saga running on the real Step Functions workflow in AWS, driven through the local API. */
export function AwsSaga() {
  const store = useAppStore();
  const [plans, setPlans] = useState<FailurePlans>({});
  const [amount, setAmount] = useState("120");

  const machine = useMemo(() => {
    const call = async <T,>(fn: () => Promise<T>): Promise<T> => {
      try { return await fn(); } catch (e) { throw toError(e); }
    };
    return remoteSagaMachine.provide({
      actors: {
        startSaga: fromPromise<{ name: string }, StartRequest>(({ input }) =>
          call(() => store.dispatch(catalogApi.endpoints.startSaga.initiate(input)).unwrap())),
        pollSaga: fromPromise(({ input }) =>
          call(() => store.dispatch(catalogApi.endpoints.getSaga.initiate(input.name, { forceRefetch: true })).unwrap())),
        sendReview: fromPromise<void, { name: string; approved: boolean }>(({ input }) =>
          call(async () => { await store.dispatch(catalogApi.endpoints.reviewSaga.initiate(input)).unwrap(); })),
      },
    });
  }, [store]);
  const [snap, send] = useMachine(machine);

  const s = snap.context.snapshot;
  const idle = snap.matches("idle") || snap.matches("finished") || snap.matches("failed");
  const deciding = snap.matches({ watching: "reviewing" });
  // Based on the latest snapshot, not on the machine's momentary state: the machine polls between waits,
  // and the review panel must not flicker away (or ignore clicks) while it does.
  const held = (s?.waitingReview === true && !s.finished) || deciding;
  const amountValue = Number(amount);
  const amountOk = Number.isFinite(amountValue) && amountValue > 0 && amountValue <= 50000;
  const label = snap.matches("watching") ? (held ? "waiting for review" : "running") : String(snap.value);
  const progress = s ?? { states: [], current: null, finished: false, retries: {} };
  const node = (step: Step) => {
    const { status, failures } = stepStatus(step, progress);
    return <StepNode key={step} label={step} status={status} failures={failures} />;
  };

  return (
    <section className="stack">
      <div className="panel stack">
        <p className="muted">
          Starts a real execution of the deployed Step Functions workflow in your AWS account. Each order costs a
          fraction of a cent plus one model call, and this console allows at most 30 per hour.
        </p>
        <div className="row wrap">
          <label className="plan">
            <span>Order amount (USD)</span>
            <input type="number" min={1} max={50000} value={amount} disabled={!idle}
                   onChange={(e) => setAmount(e.target.value)} aria-label="Order amount" />
          </label>
          {PRESETS.map((p) => (
            <button key={p.label} type="button" className="chip" disabled={!idle} onClick={() => setAmount(String(p.amount))}>{p.label}</button>
          ))}
        </div>
        <FaultSelectors plans={plans} onChange={setPlans} disabled={!idle} />
        <div className="row">
          <button disabled={!idle || !amountOk}
                  onClick={() => send({ type: "START", amount: amountValue, faults: toFaults(plans) })}>
            Start order on AWS
          </button>
          <button className="secondary" disabled={snap.matches("idle")} onClick={() => send({ type: "RESET" })}>Reset</button>
          <Badge tone={snap.matches("failed") ? "bad" : snap.matches("finished") ? "ok" : "muted"}>{label}</Badge>
          {snap.context.name && <span className="muted">{snap.context.name}</span>}
          {s && <span className="muted">{s.seconds}s</span>}
        </div>
      </div>

      {snap.matches("failed") && <p className="banner banner-bad" role="alert">Could not run the order: {snap.context.error}</p>}
      {snap.context.error && !snap.matches("failed") && <p className="error">{snap.context.error}</p>}

      {s?.risk?.level && (
        <div className="panel stack" aria-label="Risk assessment">
          <div className="row between">
            <strong>Risk assessment</strong>
            <Badge tone={s.risk.level === "high" ? "bad" : s.risk.level === "medium" ? "warn" : "ok"}>{s.risk.level}</Badge>
          </div>
          <ul className="plain">{s.risk.reasons.map((r) => <li key={r} className="muted">{r}</li>)}</ul>
          <span className="muted">judged by {s.risk.source}</span>
        </div>
      )}

      {held && (
        <div className="panel stack review" role="alert">
          <strong>This order is held for human review</strong>
          <p className="muted">The workflow is paused until a reviewer decides. Approve to continue the order, or reject to cancel it.</p>
          <div className="row">
            <button disabled={deciding} onClick={() => send({ type: "APPROVE" })}>Approve</button>
            <button className="secondary" disabled={deciding} onClick={() => send({ type: "REJECT" })}>Reject</button>
            {deciding && <span className="muted">Sending decision…</span>}
          </div>
        </div>
      )}

      <div className="grid2">
        <div className="panel stack">
          <strong>Forward steps</strong>
          <ol className="plain nodes">{FORWARD.map(node)}</ol>
        </div>
        <div className={`panel stack ${compensationStarted(progress) ? "" : "dim"}`}>
          <strong>Compensation</strong>
          <ol className="plain nodes">{COMPENSATION.map(node)}</ol>
        </div>
      </div>

      {s?.finished && s.outcome === "CONFIRMED" && <p className="banner banner-ok">Order confirmed.</p>}
      {s?.finished && s.outcome === "REJECTED" && <p className="banner banner-warn">Order rejected. Completed work was compensated.</p>}
      {s?.finished && !s.outcome && (
        <p className="banner banner-bad">
          {s.error === "CompensationFailed"
            ? "Compensation failed. The order was escalated to manual review and the failure alarm fires."
            : `Execution ${s.status.toLowerCase()}${s.error ? ` (${s.error})` : ""}.`}
        </p>
      )}

      {s && s.states.length > 0 && (
        <div className="panel stack">
          <strong>States visited</strong>
          <ol className="chips plain">
            {s.states.map((name, i) => <li key={`${name}-${i}`} className="chip-state">{name}</li>)}
          </ol>
        </div>
      )}
    </section>
  );
}
