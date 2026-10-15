# Objective Health, Progress, and Loop Recovery

## Contents

- Separate liveness, progress, and correctness
- Review objective health
- Choose objective dispositions
- Detect unproductive loops
- Recover and resume

## Separate Liveness, Progress, and Correctness

Use liveness to determine responsiveness, ownership, active operations, and
wait state. Never infer progress from liveness alone.

Use progress to measure reduction in remaining goal distance through:

- validated evidence
- resolved unknowns or falsified assumptions
- reduced risk
- unblocked dependencies
- advanced verifiable deliverables
- ruled-out paths with reusable evidence
- enabled decisions
- satisfied integration or acceptance criteria
- improved forecast that changes a resource decision

Use correctness to test current evidence, constraints, negative paths, policy,
and independent review needs. Permit an unsuccessful experiment to count as
progress when it decisively rules out a plausible path.

Report a progress vector rather than a fabricated percentage. Cover success
criteria, deliverables, unknowns, risks, dependencies, verification, forecast,
and budget.

## Review Objective Health

Review each active, blocked, or deferred objective at its cadence and after
linked tasks or plans become terminal, missed milestones, repeated no-delta
cycles, forecast or budget variance, changed value, procedure drift, new risk,
dependency failure, loss of authority, or goal change.

Assess:

| Dimension | Required judgment |
| --- | --- |
| Relevance | Preserve, increase, reduce, or eliminate expected value |
| Progress | Compare evidence delta with expected granularity |
| Correctness | Confirm constraints and current evidence |
| Feasibility | Identify a credible path inside current bounds |
| Strategy | Test whether the plan causes expected transitions |
| Procedures | Confirm currency, applicability, sufficiency, and effectiveness |
| Risk | Compare residual risk and reversibility with tolerance |
| Resources | Compare remaining cost with expected value and uncertainty |
| Dependencies | Confirm owner, availability, and decision timing |
| Actionability | Name the next safe useful action or bounded wait |

## Choose Objective Dispositions

Choose and record exactly one current disposition:

- **Continue:** preserve the route while evidence supports health.
- **Remediate:** change the causal model, plan, team, procedure set, evidence
  strategy, dependency treatment, or tool path; set a bound and review point.
- **Re-scope:** preserve intended value through an authorized boundary change.
- **Defer:** pause valuable but presently unactionable or lower-priority work;
  preserve state, owner, reason, resume condition or review date, and dependent
  work disposition.
- **Satisfy:** close only with required success evidence and acceptance.
- **Fail:** close when required success becomes unattainable under accepted
  conditions.
- **Cancel:** stop because the authorized sponsor withdraws the objective.
- **Abandon:** stop an infeasible, superseded, unsafe, valueless, or
  disproportionate objective without claiming success.

Execute re-scope, deferment, or abandonment only inside lifecycle authority.
Otherwise prepare a decision-ready recommendation and maintain an accurate
paused or blocked state.

Cascade the disposition to plans, tasks, schedules, waits, delegations,
forecasts, risks, and communication. Prevent indefinite deferment by assigning
a review trigger.

## Detect Unproductive Loops

Compare intent, causal hypothesis, tool or owner, normalized input, result
class, changed precondition, evidence gained, state delta, and decision enabled.
Treat two attempts as materially different only when a decision-relevant
element changes.

Diagnose stuck work after:

- repeated equivalent input and result
- recurring failure without changed hypothesis or precondition
- overlapping research without closing an evidence gap
- artifact churn without acceptance progress
- plan oscillation without new evidence
- handoff ping-pong
- review-repair cycles on the same cause
- polling without an event or wait model
- resource growth faster than durable progress

Use these defaults unless a domain rule requires an earlier stop:

- Stop immediately after an ambiguous high-impact side effect.
- Reorient before a third equivalent attempt.
- Enter recovery after three bounded no-delta cycles.
- Synthesize and checkpoint before exhausting context, tool, cost, or time
  budgets.

Do not flag known healthy long operations, event-driven waits, independent
high-risk validation, improving optimization, finite exhaustive search, or
bounded transient backoff while their expected signal remains present.

## Recover and Resume

Apply the lowest sufficient recovery rung:

1. Stop and checkpoint current truth.
2. Reobserve actual state and side effects.
3. Replace the failed assumption or causal model.
4. Retry only after a material precondition, method, input, timing, capability,
   or hypothesis changes.
5. Narrow, isolate, or move to a fresh bounded context.
6. Change source, tool, design, provider, path, or owner.
7. Backtrack or compensate to the last validated state.
8. Deliver an authorized partial outcome with explicit limits.
9. Escalate the exact missing information, authority, tradeoff, or constraint.
10. Terminate the path or objective accurately.

Before retrying, record prior failure, possible side effects, changed condition,
new success basis, attempt limit, and stop signal.

Persist goal, procedure register, objective-health view, plan, evidence,
unknowns, risk, in-flight state, last validated milestone, next action, and
review trigger. On resume, revalidate goal, authority, procedures, time-sensitive
evidence, objective disposition, and external effects before acting.
