import { assign, fromPromise, setup } from "xstate";
import type { SagaFaults, SagaStatus } from "../store/catalogApi";

export const MAX_POLL_FAILURES = 3;

export interface StartRequest {
  amount: number;
  faults: SagaFaults;
}

export interface RemoteContext {
  request: StartRequest | null;
  name: string | null;
  snapshot: SagaStatus | null;
  decision: boolean | null;
  error: string | null;
  pollFailures: number;
}

export type RemoteEvent =
  | ({ type: "START" } & StartRequest)
  | { type: "APPROVE" }
  | { type: "REJECT" }
  | { type: "RESET" };

/**
 * Lifecycle of one order running on the real workflow.
 *
 *   idle -> starting -> watching(polling <-> pausing | waitingReview -> reviewing) -> finished | failed
 *
 * `watching` polls the API until the execution ends. When the workflow pauses for human review the
 * machine waits for APPROVE or REJECT, delivers the decision, and resumes polling. A few failed polls
 * are tolerated (lab credentials can blip) before the run is reported as failed.
 */
export const remoteSagaMachine = setup({
  types: {} as { context: RemoteContext; events: RemoteEvent },
  actors: {
    // Replaced with API-backed implementations by `.provide(...)` at the call site.
    startSaga: fromPromise<{ name: string }, StartRequest>(async () => {
      throw new Error("startSaga actor not provided");
    }),
    pollSaga: fromPromise<SagaStatus, { name: string }>(async () => {
      throw new Error("pollSaga actor not provided");
    }),
    sendReview: fromPromise<void, { name: string; approved: boolean }>(async () => {
      throw new Error("sendReview actor not provided");
    }),
  },
  actions: {
    // Reset the run and remember the new request. Used from every state that may start an order.
    beginRun: assign(({ event }) =>
      event.type === "START"
        ? { request: { amount: event.amount, faults: event.faults }, name: null, snapshot: null, decision: null, error: null, pollFailures: 0 }
        : {}),
  },
  guards: {
    isFinished: ({ event }) => (event as unknown as { output: SagaStatus }).output.finished,
    needsReview: ({ event }) => (event as unknown as { output: SagaStatus }).output.waitingReview,
    canRetryPoll: ({ context }) => context.pollFailures < MAX_POLL_FAILURES,
    // The last snapshot says the workflow is paused for a reviewer.
    reviewPending: ({ context }) => context.snapshot?.waitingReview === true && !context.snapshot.finished,
  },
  delays: { pollDelay: 1000, reviewDelay: 2500 },
}).createMachine({
  id: "remoteSaga",
  initial: "idle",
  context: { request: null, name: null, snapshot: null, decision: null, error: null, pollFailures: 0 },
  on: {
    RESET: {
      target: ".idle",
      actions: assign({ request: null, name: null, snapshot: null, decision: null, error: null, pollFailures: 0 }),
    },
  },
  states: {
    idle: { on: { START: { target: "starting", actions: "beginRun" } } },
    starting: {
      invoke: {
        src: "startSaga",
        input: ({ context }) => context.request as StartRequest,
        onDone: { target: "watching", actions: assign({ name: ({ event }) => event.output.name }) },
        onError: { target: "failed", actions: assign({ error: ({ event }) => String((event.error as Error)?.message ?? event.error) }) },
      },
    },
    watching: {
      initial: "polling",
      // A decision is accepted whenever the last snapshot says a review is pending, including while a
      // poll is in flight; otherwise a click during the brief poll between waits would be lost.
      on: {
        APPROVE: { guard: "reviewPending", target: ".reviewing", actions: assign({ decision: true }) },
        REJECT: { guard: "reviewPending", target: ".reviewing", actions: assign({ decision: false }) },
      },
      states: {
        polling: {
          invoke: {
            src: "pollSaga",
            input: ({ context }) => ({ name: context.name as string }),
            onDone: [
              { guard: "isFinished", target: "#remoteSaga.finished", actions: assign({ snapshot: ({ event }) => event.output, pollFailures: 0 }) },
              { guard: "needsReview", target: "waitingReview", actions: assign({ snapshot: ({ event }) => event.output, pollFailures: 0 }) },
              { target: "pausing", actions: assign({ snapshot: ({ event }) => event.output, pollFailures: 0 }) },
            ],
            onError: [
              {
                guard: "canRetryPoll", target: "pausing",
                actions: assign({ pollFailures: ({ context }) => context.pollFailures + 1 }),
              },
              { target: "#remoteSaga.failed", actions: assign({ error: ({ event }) => String((event.error as Error)?.message ?? event.error) }) },
            ],
          },
        },
        pausing: { after: { pollDelay: "polling" } },
        waitingReview: {
          // Keep looking: someone may decide in the AWS console instead.
          after: { reviewDelay: "polling" },
        },
        reviewing: {
          invoke: {
            src: "sendReview",
            input: ({ context }) => ({ name: context.name as string, approved: context.decision === true }),
            onDone: { target: "polling", actions: assign({ error: null }) },
            // The decision may already have been delivered (or the review expired): fall back to polling.
            onError: { target: "polling", actions: assign({ error: ({ event }) => String((event.error as Error)?.message ?? event.error) }) },
          },
        },
      },
    },
    finished: { on: { START: { target: "starting", actions: "beginRun" } } },
    failed: { on: { START: { target: "starting", actions: "beginRun" } } },
  },
});
