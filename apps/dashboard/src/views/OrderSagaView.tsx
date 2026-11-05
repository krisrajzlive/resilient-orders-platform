import { useState } from "react";
import { Badge } from "../components/Badge";
import { useHealthQuery } from "../store/catalogApi";
import { AwsSaga } from "./AwsSaga";
import { SimulatedSaga } from "./SimulatedSaga";

type Mode = "aws" | "simulated";

export function OrderSagaView() {
  const health = useHealthQuery();
  const [choice, setChoice] = useState<Mode | null>(null);
  const connected = health.data?.sagaConnected ?? false;
  const mode: Mode = choice ?? (connected ? "aws" : "simulated");

  return (
    <section className="stack">
      <div className="row wrap between">
        <div className="seg" role="group" aria-label="Where the order runs">
          <button type="button" aria-pressed={mode === "aws"} onClick={() => setChoice("aws")}>On AWS</button>
          <button type="button" aria-pressed={mode === "simulated"} onClick={() => setChoice("simulated")}>Simulated</button>
        </div>
        <Badge tone={connected ? "ok" : "muted"}>{connected ? "AWS workflow connected" : "AWS workflow not connected"}</Badge>
      </div>
      {mode === "aws" && !connected ? (
        <div className="panel stack">
          <strong>The AWS workflow is not connected</strong>
          <p className="muted">
            Deploy the stack and start the API with its output file (see the README), or switch to the simulated
            workflow, which runs entirely in the browser.
          </p>
        </div>
      ) : mode === "aws" ? <AwsSaga /> : <SimulatedSaga />}
    </section>
  );
}
