import { assign, fromPromise, setup } from "xstate";
import type { SearchMode, SearchResponse } from "../store/catalogApi";

export const MAX_ATTEMPTS = 3;

export interface SearchContext {
  query: string;
  mode: SearchMode;
  attempt: number;
  result: SearchResponse | null;
  error: string | null;
}

export type SearchEvent =
  | { type: "SUBMIT"; query: string; mode: SearchMode }
  | { type: "RETRY" }
  | { type: "CLEAR" };

export interface SearchInput {
  query: string;
  mode: SearchMode;
}

/**
 * Search lifecycle as an explicit state machine.
 *
 *   idle -> searching -> results | degraded | retrying -> searching ... | failed
 *
 * - `degraded` means the API answered from a fallback path (keyword or cached); the UI keeps
 *   showing the results and offers a retry once the dependency may have recovered.
 * - Transient errors are retried with exponential backoff before surfacing `failed`.
 * - A new SUBMIT at any time supersedes the in-flight search: the latest query always wins.
 */
export const searchMachine = setup({
  types: {} as { context: SearchContext; events: SearchEvent },
  actors: {
    // Replaced with a real implementation by `searchMachine.provide(...)` at the call site.
    runSearch: fromPromise<SearchResponse, SearchInput>(async () => {
      throw new Error("runSearch actor not provided");
    }),
  },
  guards: {
    canRetry: ({ context }) => context.attempt < MAX_ATTEMPTS,
    isDegraded: ({ event }) => (event as unknown as { output: SearchResponse }).output.degraded,
  },
  delays: {
    backoff: ({ context }) => 400 * 2 ** (context.attempt - 1),
  },
}).createMachine({
  id: "search",
  initial: "idle",
  context: { query: "", mode: "hybrid", attempt: 0, result: null, error: null },
  on: {
    SUBMIT: {
      target: ".searching",
      actions: assign(({ event }) => ({
        query: event.query, mode: event.mode, attempt: 0, error: null,
      })),
    },
    CLEAR: {
      target: ".idle",
      actions: assign({ query: "", attempt: 0, result: null, error: null }),
    },
  },
  states: {
    idle: {},
    searching: {
      entry: assign({ attempt: ({ context }) => context.attempt + 1 }),
      invoke: {
        src: "runSearch",
        input: ({ context }) => ({ query: context.query, mode: context.mode }),
        onDone: [
          {
            guard: "isDegraded",
            target: "degraded",
            actions: assign({ result: ({ event }) => event.output, error: null }),
          },
          { target: "results", actions: assign({ result: ({ event }) => event.output, error: null }) },
        ],
        onError: [
          { guard: "canRetry", target: "retrying", actions: assign({ error: ({ event }) => String(event.error) }) },
          { target: "failed", actions: assign({ error: ({ event }) => String(event.error) }) },
        ],
      },
    },
    retrying: { after: { backoff: "searching" } },
    results: {},
    degraded: { on: { RETRY: { target: "searching", actions: assign({ attempt: 0 }) } } },
    failed: { on: { RETRY: { target: "searching", actions: assign({ attempt: 0 }) } } },
  },
});
