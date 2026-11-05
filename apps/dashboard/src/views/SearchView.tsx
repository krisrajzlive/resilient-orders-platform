import { useMachine } from "@xstate/react";
import { useMemo, useState, type FormEvent } from "react";
import { fromPromise } from "xstate";
import { Badge } from "../components/Badge";
import { searchMachine, type SearchInput } from "../machines/searchMachine";
import {
  catalogApi, useAskMutation, useVisualSearchMutation,
  type Hit, type SearchMode, type SearchResponse,
} from "../store/catalogApi";
import { useAppDispatch, useAppSelector, useAppStore } from "../store/hooks";
import { rememberQuery, setMode } from "../store/prefsSlice";

const MODES: { value: SearchMode; label: string }[] = [
  { value: "hybrid", label: "Hybrid" },
  { value: "vector", label: "Semantic" },
  { value: "keyword", label: "Keyword" },
];

const toError = (e: unknown) =>
  new Error(typeof e === "object" && e && "status" in e ? `HTTP ${String((e as { status: unknown }).status)}` : String(e));

function HitCard({ hit, cited }: { hit: Hit; cited?: boolean }) {
  return (
    <li className={`hit ${cited ? "hit-cited" : ""}`}>
      <div className="hit-head">
        <strong>{hit.name}</strong>
        <span className="price">${hit.price.toFixed(2)}</span>
      </div>
      <p>{hit.description}</p>
      <div className="hit-foot">
        <Badge tone="muted">{hit.category}</Badge>
        <span className="muted">{hit.productId}</span>
        {cited && <Badge tone="info">cited</Badge>}
      </div>
    </li>
  );
}

function ServedBy({ result }: { result: SearchResponse }) {
  return (
    <p className="served" role="status">
      Served by <Badge tone={result.degraded ? "warn" : "ok"}>{result.modeServed}</Badge>
      {result.degraded && <span className="muted"> fallback path: {result.reason ?? "dependency unavailable"}</span>}
    </p>
  );
}

export function SearchView() {
  const store = useAppStore();
  const dispatch = useAppDispatch();
  const mode = useAppSelector((s) => s.prefs.mode);
  const recent = useAppSelector((s) => s.prefs.recent);
  const [text, setText] = useState("");

  // The state machine owns the request lifecycle (retry, backoff, degraded, supersede);
  // Redux Toolkit Query owns the HTTP cache. The actor below is the seam between them.
  const machine = useMemo(
    () =>
      searchMachine.provide({
        actors: {
          runSearch: fromPromise<SearchResponse, SearchInput>(async ({ input }) => {
            const chaos = store.getState().prefs.chaos;
            try {
              return await dispatch(
                catalogApi.endpoints.search.initiate({ q: input.query, mode: input.mode, chaos }, { forceRefetch: true }),
              ).unwrap();
            } catch (e) {
              throw toError(e);
            }
          }),
        },
      }),
    [store, dispatch],
  );
  const [snap, send] = useMachine(machine);
  const [ask, askState] = useAskMutation();
  const [visual, visualState] = useVisualSearchMutation();

  const submit = (e?: FormEvent, q = text) => {
    e?.preventDefault();
    const query = q.trim();
    if (!query) return;
    dispatch(rememberQuery(query));
    send({ type: "SUBMIT", query, mode });
  };

  const { result, error, attempt } = snap.context;
  const asked = askState.data;

  return (
    <section className="stack">
      <form className="panel stack" onSubmit={submit}>
        <label htmlFor="q" className="label">Describe what you need</label>
        <div className="row">
          <input id="q" value={text} onChange={(e) => setText(e.target.value)}
                 placeholder="e.g. something to keep my feet dry on a muddy path" autoComplete="off" />
          <button type="submit" disabled={!text.trim()}>Search</button>
          <button type="button" className="secondary" disabled={!text.trim() || askState.isLoading}
                  onClick={() => ask(text.trim())}>
            {askState.isLoading ? "Asking…" : "Ask"}
          </button>
        </div>
        <div className="row wrap">
          <div className="seg" role="group" aria-label="Search mode">
            {MODES.map((m) => (
              <button type="button" key={m.value} aria-pressed={mode === m.value}
                      onClick={() => dispatch(setMode(m.value))}>{m.label}</button>
            ))}
          </div>
          {recent.map((r) => (
            <button type="button" key={r} className="chip" onClick={() => { setText(r); submit(undefined, r); }}>{r}</button>
          ))}
        </div>
        <div className="row wrap">
          <label className="file">
            Search by photo
            <input type="file" accept="image/png,image/jpeg"
                   onChange={(e) => e.target.files?.[0] && visual(e.target.files[0])} />
          </label>
          {visualState.isLoading && <span className="muted">Matching photo…</span>}
          {visualState.isError && <Badge tone="bad">photo search unavailable</Badge>}
        </div>
      </form>

      <div className="status-line" aria-live="polite">
        <Badge tone={snap.matches("failed") ? "bad" : snap.matches("degraded") ? "warn" : snap.matches("results") ? "ok" : "muted"}>
          {String(snap.value)}
        </Badge>
        {snap.matches("searching") && <span className="muted">Searching{attempt > 1 ? ` (attempt ${attempt})` : "…"}</span>}
        {snap.matches("retrying") && <span className="muted">Request failed, retrying… ({error})</span>}
        {snap.matches("failed") && <><span className="muted">{error}</span><button className="secondary" onClick={() => send({ type: "RETRY" })}>Retry</button></>}
        {snap.matches("degraded") && <button className="secondary" onClick={() => send({ type: "RETRY" })}>Retry full search</button>}
      </div>

      {asked && (
        <article className="panel stack" aria-label="Answer">
          <div className="row between">
            <strong>Answer</strong>
            <Badge tone={asked.degraded ? "warn" : "ok"}>
              {asked.modelUsed === "extractive" ? "extractive fallback" : asked.modelUsed.split(".").pop()}
            </Badge>
          </div>
          <p>{asked.answer}</p>
        </article>
      )}
      {askState.isError && <p className="error">Answer service unavailable.</p>}

      {result && (
        <div className="stack">
          <ServedBy result={result} />
          {result.hits.length === 0
            ? <p className="muted">No matching products.</p>
            : <ul className="hits">{result.hits.map((h) => <HitCard key={h.productId} hit={h} cited={asked?.citedProductIds.includes(h.productId)} />)}</ul>}
        </div>
      )}

      {visualState.data && (
        <div className="panel stack">
          <strong>Photo matches</strong>
          <ol className="plain">
            {visualState.data.map((v) => (
              <li key={v.productId} className="row between">
                <span>{v.name} <span className="muted">{v.productId}</span></span>
                <span className="muted">similarity {v.score.toFixed(2)}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
