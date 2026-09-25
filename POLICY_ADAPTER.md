# Policy data adapter for L3A

The public `contracts/scoring/scoring-policy-v2.json` specifies grading weights, hard gates and required workflow events. It does **not** define arbitration facts, liability rules or refund amounts. `PolicyEngine` reads arbitration rules only from a consumed, case-scoped `get_policy` MCP response. The released `l3a-inputs-v1` bundle and live `EC_POLICY_V1` response have now been checked; the earlier normalized shape remains supported for offline fixtures.

## Released L3A bundle and live MCP shape

The released case has `case_id`, `opened_at`, `policy_version`, and `customer_request.claimed_order_id` with `customer_request.claims[]` entries containing `claim_id` and `topic`. The authoritative live policy contains `currency`, `policy_version`, and `rules[issue]` with `case_status`, `recommended_action`, `refund_brl`, and `responsible_parties`. This differs from the normalized fixture below.

The live order uses `order_purchase_timestamp`, `order_status`, and delivery timestamps. Items and order payments arrive as arrays, with `order_item_id`, `payment_sequential`, and `payment_value`. `get_payment_timeline` supplies timestamped captured and reconciliation events; `get_refund_timeline` supplies refund events; `get_shipment_summary` supplies shipping limits and delivery timestamps. The adapter only uses timestamped events from purchase through `opened_at`, and determines whether delivery was overdue *at case opening*. It does not treat a later delivery or an unrelated earlier/later payment as evidence available at opening. An absent refund timeline is not assumed to mean an empty timeline when that fact is needed for a refund-status decision.

For each issue, the adapter checks an order/payment/shipment/refund predicate before using the MCP policy's action, party and exact BRL amount. If required evidence does not substantiate the claimed issue, the output is `insufficient_evidence` with zero refund. Public evidence envelopes and refs are never modified.

## Normalized offline fixture input

`solve_case()` accepts `case_id`, `policy_version`, and either `order_id` or `order_ids` (a nonempty list). Optional `claims` is a list of at most five objects with `claim_id`, `issue`, and `order_id` (the order ID is optional only for a one-order case). Optional `as_of` is a timestamp with timezone for assessing an undelivered shipment. Unrecognized customer prose remains a claim to investigate; the engine does not infer structured IDs, issue codes or dates from that prose.

## Normalized offline fixture MCP `data`

Every order-scoped response must carry the requested `order_id`. All other required values are read by tool as follows. Unknown shapes fail closed or produce `insufficient_evidence`; they are not coerced into invented data.

| Tool | Fields consumed from `data` |
| --- | --- |
| `get_order` | `order_id`, `order_status`, `total_brl` |
| `get_order_items` | `order_id`, `items[]` with `item_id`, `seller_id`, `price`, `freight_value` |
| `get_order_payments` / `get_payment_timeline` | `order_id`, `payments[]` with `payment_reference`, `amount_brl`, `status`; optional `duplicate_of` on a captured duplicate |
| `get_refund_timeline` | `order_id`, `refunds[]` with `refund_id`, `payment_reference`, `amount_brl`, `status` |
| `get_shipment_summary` | `order_id`, `shipments[]` with `shipment_id`, `seller_id`, `logistics_provider_id`, `promised_at`, `delivered_at`, `seller_handoff_due_at`, `seller_handoff_at` |
| `get_policy` | `policy_version`, `currency`, `rules`, `issue_priority`, `payment_tolerance_brl`; optional `source_priority` |

Accepted payment statuses: `captured`, `pending`, `failed`, `canceled`. Accepted refund statuses: `completed`, `pending`, `failed`. Timestamps require timezone. Amounts are nonnegative BRL to whole cents; all calculations use `Decimal`. These are internal adapter expectations, not additions to any public JSON Schema.

The policy `rules` object must contain every `primary_issue` allowed by the L3A output schema. Each issue maps to `case_status`, `cause_code`, `responsible_party`, `refund_mode`, `actions`, plus `refund_reason_code` for a mode other than `none`. `issue_priority` lists every issue exactly once, with `insufficient_evidence` last and the three no-action/inconclusive options last. The allowed `refund_mode` values are `none`, `unrefunded`, `overpayment`, `duplicate`, `failed`, and `freight`. `source_priority` maps a fact name such as `total` to an ordered list of authoritative tool names. Conflicting sources without a stated priority remain unresolved.

Refund modes operate on captured payments after deducting completed and pending refunds. `duplicate` additionally checks `duplicate_of` against a captured original and deducts refunds reserved for the duplicate payment itself. `freight` caps the recommendation at remaining freight entitlement. These calculations are only authorized when the corresponding policy rule selects that mode and the necessary evidence exists.

## Validation status

The engine, verifier, confidence ceiling and observable lifecycle are covered by offline fake-gateway tests. The installed `l3a-competition-v1` case set passed `day09 validate-inputs` (100 cases); live MCP discovery and case-scoped reads succeeded. A full live `day09 run` followed by `day09 validate` produced 100 schema-valid outputs and 3460 trace events. This establishes local contract/lifecycle validity, not the private business score or server-side audit result. A future bundle or MCP shape change must be inspected again; never alter an evidence envelope, ref or public schema to make it pass.
