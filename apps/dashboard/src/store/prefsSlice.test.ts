import { describe, expect, it } from "vitest";
import { initialPrefs, prefsSlice, rememberQuery, setChaos, setMode } from "./prefsSlice";

const reduce = prefsSlice.reducer;

describe("prefs slice", () => {
  it("keeps the most recent queries first, de-duplicated case-insensitively", () => {
    let s = reduce(initialPrefs, rememberQuery("boots"));
    s = reduce(s, rememberQuery("lamp"));
    s = reduce(s, rememberQuery("BOOTS"));
    expect(s.recent).toEqual(["BOOTS", "lamp"]);
  });

  it("caps history and ignores blank queries", () => {
    let s = initialPrefs;
    for (let i = 0; i < 10; i++) s = reduce(s, rememberQuery(`q${i}`));
    s = reduce(s, rememberQuery("   "));
    expect(s.recent).toHaveLength(6);
    expect(s.recent[0]).toBe("q9");
  });

  it("stores mode and chaos selections", () => {
    const s = reduce(reduce(initialPrefs, setMode("vector")), setChaos("db-down"));
    expect(s).toMatchObject({ mode: "vector", chaos: "db-down" });
  });
});
