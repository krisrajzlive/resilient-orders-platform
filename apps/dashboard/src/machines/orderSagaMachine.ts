import { assign, fromPromise, setup } from "xstate";

export const MAX_STEP_ATTEMPTS = 3;

export type Step = "ReserveStock" | "ChargePayment" | "CreateShipment" | "RefundPayment" | "ReleaseStock";
export type LogStatus = "started" | "retrying" | "succeeded" | "failed";

export interface LogEntry {
  step: Step;
  status: LogStatus;
  attempt: number;
}

export interface OrderContext {
  orderId: string;
  attempt: number;
  log: LogEntry[];
}

export type OrderEvent = { type: "PLACE_ORDER"; orderId: string } | { type: "RESET" };

export interface StepInput {
  orderId: string;
  attempt: number;
}

const step = () =>
  fromPromise<void, StepInput>(async () => {
    throw new Error("step actor not provided");
  });

const rec = (s: Step, status: LogStatus) => ({ type: "record" as const, params: { step: s, status } });

/**
 * Order saga as a state machine. Forward steps: ReserveStock -> ChargePayment -> CreateShipment.
 * Every step is retried with backoff. When a step is out of retries, compensating steps run in
 * reverse order for the work already done:
 *
 *   ChargePayment fails   -> ReleaseStock
 *   CreateShipment fails  -> RefundPayment -> ReleaseStock
 *   ReserveStock fails    -> nothing to undo
 *
 * If a compensating step itself runs out of retries the order lands in `manualReview`, the point
 * where a real system raises an alert (SNS) instead of silently leaving inconsistent state.
 * The state names match the Step Functions version so both can be read side by side.
 */
export const orderSagaMachine = setup({
  types: {} as { context: OrderContext; events: OrderEvent },
  actors: {
    reserveStock: step(),
    chargePayment: step(),
    createShipment: step(),
    refundPayment: step(),
    releaseStock: step(),
  },
  actions: {
    record: assign(({ context }, params: { step: Step; status: LogStatus }) => ({
      log: [...context.log, { step: params.step, status: params.status, attempt: context.attempt }],
    })),
    nextAttempt: assign({ attempt: ({ context }) => context.attempt + 1 }),
    resetAttempt: assign({ attempt: 0 }),
  },
  guards: { canRetry: ({ context }) => context.attempt < MAX_STEP_ATTEMPTS },
  delays: { backoff: ({ context }) => 300 * 2 ** (context.attempt - 1) },
}).createMachine({
  id: "orderSaga",
  initial: "idle",
  context: { orderId: "", attempt: 0, log: [] },
  on: {
    RESET: { target: ".idle", actions: assign({ orderId: "", attempt: 0, log: [] }) },
  },
  states: {
    idle: {
      on: {
        PLACE_ORDER: {
          target: "ReserveStock",
          actions: assign(({ event }) => ({ orderId: event.orderId, attempt: 0, log: [] })),
        },
      },
    },

    ReserveStock: {
      entry: "nextAttempt",
      invoke: {
        src: "reserveStock",
        input: ({ context }) => ({ orderId: context.orderId, attempt: context.attempt }),
        onDone: { target: "ChargePayment", actions: [rec("ReserveStock", "succeeded"), "resetAttempt"] },
        onError: [
          { guard: "canRetry", target: "ReserveStockBackoff", actions: rec("ReserveStock", "retrying") },
          { target: "rejected", actions: rec("ReserveStock", "failed") },
        ],
      },
    },
    ReserveStockBackoff: { after: { backoff: "ReserveStock" } },

    ChargePayment: {
      entry: "nextAttempt",
      invoke: {
        src: "chargePayment",
        input: ({ context }) => ({ orderId: context.orderId, attempt: context.attempt }),
        onDone: { target: "CreateShipment", actions: [rec("ChargePayment", "succeeded"), "resetAttempt"] },
        onError: [
          { guard: "canRetry", target: "ChargePaymentBackoff", actions: rec("ChargePayment", "retrying") },
          { target: "ReleaseStock", actions: [rec("ChargePayment", "failed"), "resetAttempt"] },
        ],
      },
    },
    ChargePaymentBackoff: { after: { backoff: "ChargePayment" } },

    CreateShipment: {
      entry: "nextAttempt",
      invoke: {
        src: "createShipment",
        input: ({ context }) => ({ orderId: context.orderId, attempt: context.attempt }),
        onDone: { target: "confirmed", actions: rec("CreateShipment", "succeeded") },
        onError: [
          { guard: "canRetry", target: "CreateShipmentBackoff", actions: rec("CreateShipment", "retrying") },
          { target: "RefundPayment", actions: [rec("CreateShipment", "failed"), "resetAttempt"] },
        ],
      },
    },
    CreateShipmentBackoff: { after: { backoff: "CreateShipment" } },

    // Compensation: undo completed work in reverse order.
    RefundPayment: {
      entry: "nextAttempt",
      invoke: {
        src: "refundPayment",
        input: ({ context }) => ({ orderId: context.orderId, attempt: context.attempt }),
        onDone: { target: "ReleaseStock", actions: [rec("RefundPayment", "succeeded"), "resetAttempt"] },
        onError: [
          { guard: "canRetry", target: "RefundPaymentBackoff", actions: rec("RefundPayment", "retrying") },
          { target: "manualReview", actions: rec("RefundPayment", "failed") },
        ],
      },
    },
    RefundPaymentBackoff: { after: { backoff: "RefundPayment" } },

    ReleaseStock: {
      entry: "nextAttempt",
      invoke: {
        src: "releaseStock",
        input: ({ context }) => ({ orderId: context.orderId, attempt: context.attempt }),
        onDone: { target: "rejected", actions: rec("ReleaseStock", "succeeded") },
        onError: [
          { guard: "canRetry", target: "ReleaseStockBackoff", actions: rec("ReleaseStock", "retrying") },
          { target: "manualReview", actions: rec("ReleaseStock", "failed") },
        ],
      },
    },
    ReleaseStockBackoff: { after: { backoff: "ReleaseStock" } },

    confirmed: {},
    rejected: {},
    manualReview: {},
  },
});

export type SagaOutcome = "confirmed" | "rejected" | "manualReview";
export const TERMINAL_STATES: SagaOutcome[] = ["confirmed", "rejected", "manualReview"];
