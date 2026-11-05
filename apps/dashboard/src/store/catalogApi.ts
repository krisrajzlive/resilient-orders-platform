import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";
import type { RootState } from "./index";

export type SearchMode = "hybrid" | "vector" | "keyword";
export type ChaosMode = "none" | "embedding-down" | "db-down" | "llm-down";

export interface Hit {
  productId: string;
  name: string;
  category: string;
  description: string;
  price: number;
  score: number;
}
export interface SearchResponse {
  hits: Hit[];
  modeServed: "hybrid" | "vector" | "keyword" | "cached";
  degraded: boolean;
  reason?: string | null;
}
export interface AskResponse {
  answer: string;
  citedProductIds: string[];
  hits: Hit[];
  modelUsed: string;
  modeServed: string;
  degraded: boolean;
}
export interface VisualHit {
  productId: string;
  name: string;
  price: number;
  score: number;
}
export interface HealthResponse {
  status: "ok" | "degraded";
  products?: number;
  chaosEnabled: boolean;
  sagaConnected: boolean;
}

export type FaultKind = "transient" | "permanent";
export type SagaFaults = Partial<Record<string, FaultKind>>;

export interface SagaStatus {
  name: string;
  status: "RUNNING" | "SUCCEEDED" | "FAILED" | "TIMED_OUT" | "ABORTED";
  finished: boolean;
  outcome?: "CONFIRMED" | "REJECTED" | null;
  error?: string | null;
  states: string[];
  current?: string | null;
  retries: Record<string, number>;
  risk?: { level?: string | null; reasons: string[]; source?: string | null } | null;
  waitingReview: boolean;
  seconds: number;
}

export const catalogApi = createApi({
  reducerPath: "catalogApi",
  baseQuery: fetchBaseQuery({
    baseUrl: "/api",
    // Every request carries the chaos setting chosen in the UI, so the whole app can be
    // run against a failing dependency without touching the backend.
    prepareHeaders: (headers, { getState }) => {
      const chaos = (getState() as RootState).prefs.chaos;
      if (chaos !== "none") headers.set("X-Chaos", chaos);
      return headers;
    },
  }),
  endpoints: (build) => ({
    search: build.query<SearchResponse, { q: string; mode: SearchMode; chaos: ChaosMode }>({
      // `chaos` is part of the cache key so toggling it never serves a stale healthy result.
      query: ({ q, mode }) => ({ url: "search", params: { q, mode, limit: 6 } }),
    }),
    ask: build.mutation<AskResponse, string>({
      query: (question) => ({ url: "ask", method: "POST", body: { question } }),
    }),
    visualSearch: build.mutation<VisualHit[], File>({
      query: (file) => {
        const body = new FormData();
        body.append("image", file);
        return { url: "visual-search", method: "POST", body };
      },
    }),
    health: build.query<HealthResponse, void>({ query: () => "health" }),
    // Order saga on AWS Step Functions (through the local API).
    startSaga: build.mutation<{ name: string }, { amount: number; faults: SagaFaults }>({
      query: (body) => ({ url: "saga/executions", method: "POST", body }),
    }),
    getSaga: build.query<SagaStatus, string>({ query: (name) => `saga/executions/${name}` }),
    reviewSaga: build.mutation<{ delivered: boolean }, { name: string; approved: boolean }>({
      query: ({ name, approved }) => ({ url: `saga/executions/${name}/review`, method: "POST", body: { approved } }),
    }),
  }),
});

export const { useSearchQuery, useAskMutation, useVisualSearchMutation, useHealthQuery } = catalogApi;
