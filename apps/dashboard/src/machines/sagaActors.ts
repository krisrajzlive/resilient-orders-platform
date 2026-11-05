import { fromPromise } from "xstate";
import { MAX_STEP_ATTEMPTS, type Step, type StepInput } from "./orderSagaMachine";

/** How a simulated step behaves: always works, fails until its last allowed attempt, or never works. */
export type FailurePlan = "none" | "transient" | "permanent";
export type FailurePlans = Partial<Record<Step, FailurePlan>>;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/**
 * Simulated step implementations for the console. The same machine runs unchanged against
 * real backends (Step Functions executions, the LangGraph service) by providing different actors.
 */
export function sagaActors(plans: FailurePlans | (() => FailurePlans), delayMs = 600) {
  const make = (name: Step) =>
    fromPromise<void, StepInput>(async ({ input }) => {
      await sleep(delayMs);
      // A getter lets the UI change the plan between orders without recreating the machine.
      const plan = (typeof plans === "function" ? plans() : plans)[name] ?? "none";
      const fails = plan === "permanent" || (plan === "transient" && input.attempt < MAX_STEP_ATTEMPTS);
      if (fails) throw new Error(`${name} failed (attempt ${input.attempt})`);
    });
  return {
    reserveStock: make("ReserveStock"),
    chargePayment: make("ChargePayment"),
    createShipment: make("CreateShipment"),
    refundPayment: make("RefundPayment"),
    releaseStock: make("ReleaseStock"),
  };
}
