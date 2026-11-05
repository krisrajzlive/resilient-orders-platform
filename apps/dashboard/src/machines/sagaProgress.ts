import type { SagaStatus } from "../store/catalogApi";
import type { Step } from "./orderSagaMachine";

export type NodeStatus = "waiting" | "active" | "retrying" | "done" | "failed";

export const FORWARD: Step[] = ["ReserveStock", "ChargePayment", "CreateShipment"];
export const COMPENSATION: Step[] = ["RefundPayment", "ReleaseStock"];

// The state a step moves to when it succeeds. Anything else after it means it failed.
const SUCCESS_NEXT: Record<Step, string> = {
  ReserveStock: "ChargePayment",
  ChargePayment: "CreateShipment",
  CreateShipment: "MarkConfirmed",
  RefundPayment: "ReleaseStock",
  ReleaseStock: "MarkRejected",
};

type Progress = Pick<SagaStatus, "states" | "current" | "finished" | "retries">;

/** Status of one step, derived from the states a real execution has visited so far. */
export function stepStatus(step: Step, p: Progress): { status: NodeStatus; failures: number } {
  const failures = p.retries[step] ?? 0;
  const at = p.states.lastIndexOf(step);
  if (at < 0) return { status: "waiting", failures };
  const next = p.states[at + 1];
  if (next === undefined) {
    if (!p.finished && p.current === step) return { status: failures > 0 ? "retrying" : "active", failures };
    return { status: p.finished ? "failed" : "active", failures };
  }
  return { status: next === SUCCESS_NEXT[step] ? "done" : "failed", failures };
}

export const compensationStarted = (p: Pick<SagaStatus, "states">) =>
  p.states.some((s) => COMPENSATION.includes(s as Step));
