# MAGI Deliberation

MAGI deliberation is an optional Shadow chat mode. When enabled, a user message
fans out to three configured chat models, then resolves through either voting or
judge synthesis.

## Roles

- `MELCHIOR-01`: logic-first scientist. Precise, evidence-driven, explicit about uncertainty.
- `BALTHASAR-02`: protective pragmatic operator. Weighs safety, cost, reliability, and next action.
- `CASPER-03`: intuition-led skeptic. Challenges assumptions and catches hidden failure modes.

The personas are intentionally light. They bias review style without replacing
normal factual answering.

## Resolution Modes

- `vote`: no fourth model call. The majority stance wins when possible.
- `debate`: runs the normal three independent answers, then a peer-review round
  where each surviving role sees the other two before the final vote.
- `judge`: runs a synthesis call over the three role answers. If the judge call
  fails, Shadow returns the vote result as `resolution: "vote_fallback"`.

## Architecture

MAGI is split into three layers:

- `routes/magi_routes.py`: authentication, ownership checks, provider/model target resolution, and chat-session persistence.
- `src/magi_orchestrator.py`: fan-out, degraded execution, debate round, vote resolution, judge fallback, and structured response assembly. It accepts an injected model caller, so it can be tested without importing the web stack.
- `src/magi_deliberation.py`: pure parsing, prompts, vote math, and markdown formatting helpers.

This keeps normal chat independent from MAGI and makes the deliberation engine portable if a future service boundary is needed.

## Structured Payload

`POST /api/magi/deliberate`

```json
{
  "query": "Should we deploy this change?",
  "display_query": "Should we deploy this change?",
  "session_id": "optional-current-chat-session",
  "mode": "vote",
  "roles": [
    {"role": "melchior", "endpoint_id": "...", "model": "..."},
    {"role": "balthasar", "endpoint_id": "...", "model": "..."},
    {"role": "casper", "endpoint_id": "...", "model": "..."}
  ]
}
```

Response:

```json
{
  "final": "MAGI vote: APPROVE (2/3 answered). Split: APPROVE=2, REJECT=1.",
  "mode": "vote",
  "resolution": "vote",
  "magi": [
    {"role": "melchior", "model": "...", "answer": "...", "stance": "APPROVE", "confidence": 82, "risks": ["..."], "dissent": "...", "next_step": "...", "ok": true},
    {"role": "balthasar", "model": "...", "answer": "...", "stance": "APPROVE", "ok": true},
    {"role": "casper", "model": "...", "answer": "...", "stance": "REJECT", "ok": true}
  ],
  "agreement": "majority",
  "degraded": false
}
```

`agreement` is one of `unanimous`, `majority`, or `split`. If one model fails,
Shadow still returns the successful responses and marks `degraded: true`.

## Role Response Contract

Each role is prompted to return strict JSON with:

- `stance`: `APPROVE`, `REJECT`, `CONDITIONAL`, or `ANSWER`
- `confidence`: integer 0-100 when available
- `answer`: concise answer or verdict
- `risks`: concrete risk notes
- `dissent`: what this role thinks the other roles may miss
- `next_step`: the most useful immediate action

Partial failure is tolerated. If one role times out or errors, Shadow continues
with the remaining roles and marks the result degraded.
