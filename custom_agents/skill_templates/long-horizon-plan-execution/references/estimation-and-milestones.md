# Estimation and Milestones

## Estimate Honest Ranges

Separate effort from elapsed time. Include discovery, implementation,
integration, verification, review, rework, handoffs, external waits, and
operationalization.

Use analogous work, bottom-up decomposition, bounded experiments, throughput,
or three-point estimates as evidence. Avoid unsupported point estimates.

Record:

- scope and exclusions
- optimistic, likely, and pessimistic cases
- effort and elapsed-time ranges
- confidence and basis
- assumptions and external waits
- controlling dependency and critical path
- contingency and re-estimation triggers

Apply three-point reasoning when useful:

`expected = (optimistic + 4 * likely + pessimistic) / 6`

`spread = (pessimistic - optimistic) / 6`

Treat the result as a planning aid rather than guaranteed precision.

## Re-estimate from Evidence

Re-estimate after scope, procedure, dependency, resource, quality, or risk
changes; after a milestone variance exceeds tolerance; or after observed
throughput invalidates the estimate basis.

Record original range, actual state, variance, cause, revised range, confidence
change, and decision impact. Preserve estimation history instead of rewriting
the original prediction.

## Set Evidence-Bearing Milestones

Define milestones as observable state transitions, uncertainty reductions, or
decision gates. Reject activity labels such as “finish research” or “complete
API work” without evidence and acceptance conditions.

Require every milestone to answer:

1. What fact becomes true?
2. How will the team know?
3. Who accepts the evidence?
4. What decision or work becomes possible next?
5. What happens when the evidence fails?

Use discovery, design, capability, integration, verification, delivery,
acceptance, operational, and learning milestones as appropriate.

Include:

- meaningful state and value
- entry conditions and deliverables
- applicable procedures and required evidence
- evaluator or decision owner
- target range and confidence
- dependencies and risks
- decision enabled and failure response
- user update requirement

## Forecast the Controlling Path

Map hard dependencies and external waits. Identify the chain controlling the
next useful outcome or deadline. Avoid treating all parallel tasks as equally
schedule-critical.

Communicate forecasts as ranges with confidence and latest useful decision
times. Offer explicit scope, quality, resource, sequence, or date tradeoffs
when the deadline is infeasible. Never hide required verification to create an
on-time appearance.
