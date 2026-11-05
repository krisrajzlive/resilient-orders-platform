import type { ReactNode } from "react";

export type Tone = "ok" | "warn" | "bad" | "info" | "muted";

export function Badge({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}
