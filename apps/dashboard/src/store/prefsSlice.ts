import { createSlice, type PayloadAction } from "@reduxjs/toolkit";
import type { ChaosMode, SearchMode } from "./catalogApi";

export interface PrefsState {
  mode: SearchMode;
  chaos: ChaosMode;
  recent: string[];
}

export const initialPrefs: PrefsState = { mode: "hybrid", chaos: "none", recent: [] };

const MAX_RECENT = 6;

export const prefsSlice = createSlice({
  name: "prefs",
  initialState: initialPrefs,
  reducers: {
    setMode: (s, a: PayloadAction<SearchMode>) => {
      s.mode = a.payload;
    },
    setChaos: (s, a: PayloadAction<ChaosMode>) => {
      s.chaos = a.payload;
    },
    // Most recent first, de-duplicated case-insensitively, capped.
    rememberQuery: (s, a: PayloadAction<string>) => {
      const q = a.payload.trim();
      if (!q) return;
      s.recent = [q, ...s.recent.filter((r) => r.toLowerCase() !== q.toLowerCase())].slice(0, MAX_RECENT);
    },
  },
});

export const { setMode, setChaos, rememberQuery } = prefsSlice.actions;
