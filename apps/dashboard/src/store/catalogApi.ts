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
  }),
});

export const { useSearchQuery, useAskMutation, useVisualSearchMutation, useHealthQuery } = catalogApi;
