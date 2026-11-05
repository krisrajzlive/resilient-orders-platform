import { configureStore } from "@reduxjs/toolkit";
import { catalogApi } from "./catalogApi";
import { prefsSlice } from "./prefsSlice";

export function makeStore() {
  return configureStore({
    reducer: {
      prefs: prefsSlice.reducer,
      [catalogApi.reducerPath]: catalogApi.reducer,
    },
    middleware: (getDefault) => getDefault().concat(catalogApi.middleware),
  });
}

export const store = makeStore();
export type AppStore = ReturnType<typeof makeStore>;
export type RootState = ReturnType<AppStore["getState"]>;
export type AppDispatch = AppStore["dispatch"];
