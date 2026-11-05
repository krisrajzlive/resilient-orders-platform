import { createActor, waitFor } from "xstate";
import { describe, expect, it } from "vitest";
import { orderSagaMachine, TERMINAL_STATES, type Step } from "./orderSagaMachine";
import { sagaActors, type FailurePlans } from "./sagaActors";

async function run(plans: FailurePlans) {
  const machine = orderSagaMachine.provide({
    actors: sagaActors(plans, 0),
    delays: { backoff: 0 },
  });
  const actor = createActor(machine).start();
  actor.send({ type: "PLACE_ORDER", orderId: "o-1" });
  const snap = await waitFor(actor, (s) => TERMINAL_STATES.some((t) => s.matches(t)), { timeout: 2000 });
  actor.stop();
  const outcome = TERMINAL_STATES.find((t) => snap.matches(t));
  const succeeded = snap.context.log.filter((l) => l.status === "succeeded").map((l) => l.step);
  return { outcome, succeeded, log: snap.context.log };
}

describe("order saga", () => {
  it("confirms when every step succeeds", async () => {
    const r = await run({});
    expect(r.outcome).toBe("confirmed");
    expect(r.succeeded).toEqual<Step[]>(["ReserveStock", "ChargePayment", "CreateShipment"]);
  });

  it("retries transient failures and still confirms", async () => {
    const r = await run({ ChargePayment: "transient" });
    expect(r.outcome).toBe("confirmed");
    expect(r.log.filter((l) => l.step === "ChargePayment" && l.status === "retrying")).toHaveLength(2);
  });

  it("releases stock when payment permanently fails", async () => {
    const r = await run({ ChargePayment: "permanent" });
    expect(r.outcome).toBe("rejected");
    expect(r.succeeded).toEqual<Step[]>(["ReserveStock", "ReleaseStock"]);
  });

  it("refunds then releases stock when shipping permanently fails", async () => {
    const r = await run({ CreateShipment: "permanent" });
    expect(r.outcome).toBe("rejected");
    expect(r.succeeded).toEqual<Step[]>(["ReserveStock", "ChargePayment", "RefundPayment", "ReleaseStock"]);
  });

  it("needs no compensation when the first step fails", async () => {
    const r = await run({ ReserveStock: "permanent" });
    expect(r.outcome).toBe("rejected");
    expect(r.succeeded).toEqual([]);
  });

  it("escalates to manual review when compensation itself keeps failing", async () => {
    const r = await run({ CreateShipment: "permanent", RefundPayment: "permanent" });
    expect(r.outcome).toBe("manualReview");
    expect(r.log.at(-1)).toMatchObject({ step: "RefundPayment", status: "failed" });
  });

  it("can be reset after finishing", async () => {
    const actor = createActor(orderSagaMachine.provide({ actors: sagaActors({}, 0), delays: { backoff: 0 } })).start();
    actor.send({ type: "PLACE_ORDER", orderId: "o-2" });
    await waitFor(actor, (s) => s.matches("confirmed"), { timeout: 2000 });
    actor.send({ type: "RESET" });
    expect(actor.getSnapshot().value).toBe("idle");
    expect(actor.getSnapshot().context.log).toEqual([]);
  });
});
