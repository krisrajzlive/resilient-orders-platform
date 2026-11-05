import { createActor, fromPromise, waitFor } from "xstate";
import { describe, expect, it } from "vitest";
import { searchMachine, type SearchInput } from "./searchMachine";
import type { SearchResponse } from "../store/catalogApi";

const ok = (degraded = false): SearchResponse => ({
  hits: [], modeServed: degraded ? "keyword" : "hybrid", degraded, reason: degraded ? "vector_unavailable" : null,
});

function start(impl: (input: SearchInput, call: number) => Promise<SearchResponse>) {
  let call = 0;
  const machine = searchMachine.provide({
    actors: { runSearch: fromPromise<SearchResponse, SearchInput>(({ input }) => impl(input, ++call)) },
    delays: { backoff: 0 },
  });
  return { actor: createActor(machine).start(), calls: () => call };
}

describe("search machine", () => {
  it("goes idle -> results on a healthy answer", async () => {
    const { actor } = start(async () => ok());
    actor.send({ type: "SUBMIT", query: "boots", mode: "hybrid" });
    const snap = await waitFor(actor, (s) => s.matches("results"));
    expect(snap.context.result?.modeServed).toBe("hybrid");
  });

  it("lands in degraded when the API served a fallback, keeping the results", async () => {
    const { actor } = start(async () => ok(true));
    actor.send({ type: "SUBMIT", query: "boots", mode: "hybrid" });
    const snap = await waitFor(actor, (s) => s.matches("degraded"));
    expect(snap.context.result?.reason).toContain("vector_unavailable");
  });

  it("retries transient errors with backoff and then succeeds", async () => {
    const { actor, calls } = start(async (_i, n) => {
      if (n < 3) throw new Error("network");
      return ok();
    });
    actor.send({ type: "SUBMIT", query: "boots", mode: "hybrid" });
    await waitFor(actor, (s) => s.matches("results"), { timeout: 2000 });
    expect(calls()).toBe(3);
  });

  it("fails after the retry budget is spent, and RETRY starts a fresh budget", async () => {
    const { actor, calls } = start(async () => {
      throw new Error("down");
    });
    actor.send({ type: "SUBMIT", query: "boots", mode: "hybrid" });
    const failed = await waitFor(actor, (s) => s.matches("failed"), { timeout: 2000 });
    expect(calls()).toBe(3);
    expect(failed.context.error).toContain("down");
    actor.send({ type: "RETRY" });
    await waitFor(actor, (s) => s.matches("failed") && calls() === 6, { timeout: 2000 });
  });

  it("lets the latest query supersede an in-flight search", async () => {
    const { actor } = start(
      (input) =>
        new Promise((resolve) => setTimeout(() => resolve({ ...ok(), reason: input.query }), input.query === "slow" ? 60 : 5)),
    );
    actor.send({ type: "SUBMIT", query: "slow", mode: "hybrid" });
    actor.send({ type: "SUBMIT", query: "fast", mode: "keyword" });
    const snap = await waitFor(actor, (s) => s.matches("results"));
    await new Promise((r) => setTimeout(r, 100));
    expect(actor.getSnapshot().context.query).toBe("fast");
    expect(actor.getSnapshot().context.result?.reason).toBe("fast");
    expect(snap.context.mode).toBe("keyword");
  });

  it("clears back to idle", async () => {
    const { actor } = start(async () => ok());
    actor.send({ type: "SUBMIT", query: "boots", mode: "hybrid" });
    await waitFor(actor, (s) => s.matches("results"));
    actor.send({ type: "CLEAR" });
    expect(actor.getSnapshot().value).toBe("idle");
    expect(actor.getSnapshot().context.result).toBeNull();
  });
});
