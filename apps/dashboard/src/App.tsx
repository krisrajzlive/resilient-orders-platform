import { useState } from "react";
import { Badge } from "./components/Badge";
import { useHealthQuery, type ChaosMode } from "./store/catalogApi";
import { useAppDispatch, useAppSelector } from "./store/hooks";
import { setChaos } from "./store/prefsSlice";
import { OrderSagaView } from "./views/OrderSagaView";
import { SearchView } from "./views/SearchView";

const CHAOS: { value: ChaosMode; label: string }[] = [
  { value: "none", label: "Healthy" },
  { value: "embedding-down", label: "Embedding model down" },
  { value: "db-down", label: "Database down" },
  { value: "llm-down", label: "Language model down" },
];

export function App() {
  const [tab, setTab] = useState<"search" | "saga">("search");
  const dispatch = useAppDispatch();
  const chaos = useAppSelector((s) => s.prefs.chaos);
  const health = useHealthQuery(undefined, { pollingInterval: 15000 });

  return (
    <div className="shell">
      <header className="top">
        <div>
          <h1>Resilient Orders Console</h1>
          <p className="muted">Catalog search, grounded answers and order workflows</p>
        </div>
        <div className="row wrap">
          <Badge tone={health.isError ? "bad" : health.data?.status === "ok" ? "ok" : "warn"}>
            {health.isError ? "API unreachable" : health.data ? `API ${health.data.status} · ${health.data.products ?? "?"} products` : "checking…"}
          </Badge>
          {health.data?.chaosEnabled && (
            <label className="chaos">
              Fault injection
              <select value={chaos} onChange={(e) => dispatch(setChaos(e.target.value as ChaosMode))}>
                {CHAOS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
              </select>
            </label>
          )}
        </div>
      </header>

      <nav className="tabs" role="tablist">
        <button role="tab" aria-selected={tab === "search"} onClick={() => setTab("search")}>Catalog search</button>
        <button role="tab" aria-selected={tab === "saga"} onClick={() => setTab("saga")}>Order saga</button>
      </nav>

      <main>{tab === "search" ? <SearchView /> : <OrderSagaView />}</main>
    </div>
  );
}
