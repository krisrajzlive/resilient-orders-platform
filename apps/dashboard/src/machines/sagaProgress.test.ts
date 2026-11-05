import { describe, expect, it } from "vitest";
import { compensationStarted, stepStatus } from "./sagaProgress";

const base = { finished: false, current: null as string | null, retries: {} as Record<string, number> };

describe("step status from a real execution", () => {
  it("marks steps done once the workflow moved on, and the current step active", () => {
    const p = { ...base, states: ["AssessRisk", "RiskChoice", "ReserveStock", "ChargePayment"], current: "ChargePayment" };
    expect(stepStatus("ReserveStock", p).status).toBe("done");
    expect(stepStatus("ChargePayment", p).status).toBe("active");
    expect(stepStatus("CreateShipment", p).status).toBe("waiting");
  });

  it("shows a step as retrying when it has failed attempts and is still the current state", () => {
    const p = { ...base, states: ["ReserveStock", "ChargePayment"], current: "ChargePayment", retries: { ChargePayment: 2 } };
    expect(stepStatus("ChargePayment", p)).toEqual({ status: "retrying", failures: 2 });
  });

  it("marks a step failed when the workflow went to compensation instead of the next step", () => {
    const states = ["ReserveStock", "ChargePayment", "CreateShipment", "RefundPayment", "ReleaseStock", "MarkRejected"];
    const p = { ...base, states, finished: true, current: "OrderRejected", retries: { CreateShipment: 1 } };
    expect(stepStatus("CreateShipment", p).status).toBe("failed");
    expect(stepStatus("RefundPayment", p).status).toBe("done");
    expect(stepStatus("ReleaseStock", p).status).toBe("done");
    expect(compensationStarted(p)).toBe(true);
  });

  it("marks a compensating step failed when the workflow escalated to manual review", () => {
    const states = ["CreateShipment", "RefundPayment", "ManualReview", "CompensationFailed"];
    const p = { ...base, states, finished: true, current: "CompensationFailed" };
    expect(stepStatus("RefundPayment", p).status).toBe("failed");
    expect(stepStatus("ReleaseStock", p).status).toBe("waiting");
  });

  it("does not report compensation before it starts", () => {
    expect(compensationStarted({ states: ["ReserveStock", "ChargePayment"] })).toBe(false);
  });
});
