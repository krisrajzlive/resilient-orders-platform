import { createActor, fromPromise, waitFor } from "xstate";
import { describe, expect, it } from "vitest";
import { remoteSagaMachine, type StartRequest } from "./remoteSagaMachine";
import type { SagaStatus } from "../store/catalogApi";

const status = (over: Partial<SagaStatus> = {}): SagaStatus => ({
  name: "ui-1", status: "RUNNING", finished: false, states: [], retries: {}, waitingReview: false, seconds: 1, ...over,
});

interface Script {
  polls: (SagaStatus | Error)[];
  start?: () => Promise<{ name: string }>;
  review?: (name: string, approved: boolean) => Promise<void>;
}

function start(script: Script) {
  const log = { started: [] as StartRequest[], reviews: [] as [string, boolean][], polls: 0 };
  const machine = remoteSagaMachine.provide({
    actors: {
      startSaga: fromPromise(async ({ input }) => {
        log.started.push(input);
        return script.start ? script.start() : { name: "ui-1" };
      }),
      pollSaga: fromPromise(async () => {
        const next = script.polls[Math.min(log.polls++, script.polls.length - 1)];
        if (next instanceof Error) throw next;
        return next;
      }),
      sendReview: fromPromise(async ({ input }) => {
        log.reviews.push([input.name, input.approved]);
        if (script.review) await script.review(input.name, input.approved);
      }),
    },
    delays: { pollDelay: 0, reviewDelay: 5 },
  });
  return { actor: createActor(machine).start(), log };
}

const ORDER = { type: "START", amount: 120, faults: {} } as const;

describe("remote saga machine", () => {
  it("starts an order, polls until the execution ends and keeps the final snapshot", async () => {
    const { actor, log } = start({ polls: [status(), status({ current: "ChargePayment" }),
      status({ status: "SUCCEEDED", finished: true, outcome: "CONFIRMED" })] });
    actor.send({ ...ORDER, amount: 250, faults: { ChargePayment: "transient" } });
    const snap = await waitFor(actor, (s) => s.matches("finished"), { timeout: 2000 });
    expect(log.started).toEqual([{ amount: 250, faults: { ChargePayment: "transient" } }]);
    expect(snap.context.snapshot?.outcome).toBe("CONFIRMED");
    expect(log.polls).toBe(3);
  });

  it("pauses for human review and resumes after the decision is delivered", async () => {
    const { actor, log } = start({ polls: [status({ current: "HumanReview", waitingReview: true }),
      status({ current: "HumanReview", waitingReview: true }),
      status({ status: "SUCCEEDED", finished: true, outcome: "CONFIRMED" })] });
    actor.send(ORDER);
    await waitFor(actor, (s) => s.matches({ watching: "waitingReview" }), { timeout: 2000 });
    actor.send({ type: "APPROVE" });
    await waitFor(actor, (s) => s.matches("finished"), { timeout: 2000 });
    expect(log.reviews).toEqual([["ui-1", true]]);
  });

  it("accepts a decision while a poll is in flight, as long as the last snapshot says the order is held", async () => {
    // The reviewer clicks while the machine is between "waitingReview" and its next poll: that must not be lost.
    let release: (s: SagaStatus) => void = () => {};
    const held = status({ waitingReview: true, current: "HumanReview" });
    const { actor, log } = start({ polls: [held, new Promise<SagaStatus>((r) => { release = r; }) as unknown as SagaStatus] });
    actor.send(ORDER);
    await waitFor(actor, (s) => s.matches({ watching: "waitingReview" }), { timeout: 2000 });
    await waitFor(actor, (s) => s.matches({ watching: "polling" }), { timeout: 2000 });      // reviewDelay elapsed, poll in flight
    actor.send({ type: "APPROVE" });
    await waitFor(actor, (s) => s.matches({ watching: "reviewing" }), { timeout: 2000 });
    expect(log.reviews).toEqual([["ui-1", true]]);
    release(status({ status: "SUCCEEDED", finished: true }));
  });

  it("ignores a decision when no review is pending", async () => {
    const { actor, log } = start({ polls: [status({ current: "ChargePayment" })] });
    actor.send(ORDER);
    await waitFor(actor, (s) => s.matches("watching"), { timeout: 2000 });
    actor.send({ type: "APPROVE" });
    await new Promise((r) => setTimeout(r, 30));
    expect(log.reviews).toEqual([]);
    expect(actor.getSnapshot().matches({ watching: "reviewing" })).toBe(false);
  });

  it("delivers a rejection as approved=false", async () => {
    const { actor, log } = start({ polls: [status({ waitingReview: true, current: "HumanReview" }),
      status({ status: "SUCCEEDED", finished: true, outcome: "REJECTED" })] });
    actor.send(ORDER);
    await waitFor(actor, (s) => s.matches({ watching: "waitingReview" }), { timeout: 2000 });
    actor.send({ type: "REJECT" });
    const snap = await waitFor(actor, (s) => s.matches("finished"), { timeout: 2000 });
    expect(log.reviews[0][1]).toBe(false);
    expect(snap.context.snapshot?.outcome).toBe("REJECTED");
  });

  it("tolerates a few failed polls but gives up after the budget", async () => {
    const blip = new Error("network");
    const ok = start({ polls: [blip, blip, status({ status: "SUCCEEDED", finished: true })] });
    ok.actor.send(ORDER);
    await waitFor(ok.actor, (s) => s.matches("finished"), { timeout: 2000 });

    const dead = start({ polls: [blip] });
    dead.actor.send(ORDER);
    const snap = await waitFor(dead.actor, (s) => s.matches("failed"), { timeout: 2000 });
    expect(snap.context.error).toContain("network");
    expect(dead.log.polls).toBe(4);                         // first call plus three retries
  });

  it("fails cleanly when the order cannot be started (for example the hourly cap)", async () => {
    const { actor, log } = start({ polls: [status()], start: async () => { throw new Error("HTTP 429: at most 30 executions per hour"); } });
    actor.send(ORDER);
    const snap = await waitFor(actor, (s) => s.matches("failed"), { timeout: 2000 });
    expect(snap.context.error).toContain("429");
    expect(log.polls).toBe(0);
  });

  it("falls back to polling when a review decision cannot be delivered", async () => {
    const { actor } = start({
      polls: [status({ waitingReview: true, current: "HumanReview" }), status({ status: "SUCCEEDED", finished: true })],
      review: async () => { throw new Error("HTTP 404: no review is waiting for this order"); },
    });
    actor.send(ORDER);
    await waitFor(actor, (s) => s.matches({ watching: "waitingReview" }), { timeout: 2000 });
    actor.send({ type: "APPROVE" });
    const snap = await waitFor(actor, (s) => s.matches("finished"), { timeout: 2000 });
    expect(snap.context.error).toContain("404");
  });

  it("starts a second order with its own request after the first finished", async () => {
    const { actor, log } = start({ polls: [status({ status: "SUCCEEDED", finished: true })] });
    actor.send({ ...ORDER, amount: 100 });
    await waitFor(actor, (s) => s.matches("finished"), { timeout: 2000 });
    actor.send({ ...ORDER, amount: 9000, faults: { CreateShipment: "permanent" } });
    await waitFor(actor, (s) => s.matches("finished") && log.started.length === 2, { timeout: 2000 });
    expect(log.started[1]).toEqual({ amount: 9000, faults: { CreateShipment: "permanent" } });
  });

  it("resets to idle from any state", async () => {
    const { actor } = start({ polls: [status()] });
    actor.send(ORDER);
    await waitFor(actor, (s) => s.matches("watching"), { timeout: 2000 });
    actor.send({ type: "RESET" });
    expect(actor.getSnapshot().value).toBe("idle");
    expect(actor.getSnapshot().context.name).toBeNull();
  });
});
