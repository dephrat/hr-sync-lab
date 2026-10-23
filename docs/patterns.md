# Patterns worth reusing

Written for another team building agents and automations on top of business systems. Each pattern says when it applies, what it looks like, and where it lives in this repo so you can read the real code.

## 1. Idempotent writer with a terminal-row marker

**When to use.** Any job that applies a feed of events to another system and can be interrupted and rerun: a scheduled sync, a queue consumer, an agent that retries tool calls.

**The shape.** Do not keep a separate "processed" flag. Write an audit row for every step, and pick a small set of (action, outcome) pairs that mean "this event is finished". Before processing an event, look for one of those rows. If none exists, process the event from the top, even if some steps succeeded last time. That only works if every step is safe to repeat, so make each one so: look up before you create, send only the fields that differ, set absolute values rather than increments. Advance the input watermark only after a whole batch finished without a failure.

**Where.** `hrsync/store.py:already_processed` holds the terminal set (`verify_enrollment` ok, `terminate_learner` ok, `queue` queued). `hrsync/engine.py:process_change` shows each step made repeatable, and `engine.run` shows the watermark rule. Rationale in [decision 0004](decisions/0004-idempotency-key.md).

## 2. Append-only audit row

**When to use.** Whenever someone will later ask "what did the automation do to this person, and why?". That includes an agent answering the question for them.

**The shape.** One table, insert only, one row per step:

```
run_id | change_id | worker_id | learner_id | action | outcome | detail | created_at
```

`action` is a fixed verb (`map_learner`, `create_learner`, `patch_learner`, `verify_enrollment`, `terminate_learner`, `queue`, `skip`); `outcome` is `ok`, `skipped`, `queued` or `failed`; `detail` holds what was sent (the patch diff as JSON) or why it failed. A `UNIQUE(run_id, change_id, action)` constraint makes a double write within one run an error instead of a silent duplicate. The run id is a timestamp plus random suffix, so reruns add rows and never rewrite history.

**Where.** Schema in `hrsync/store.py:SCHEMA`; writes through `store.audit`. The MCP tools `get_audit_trail` and `explain_gap` (`mcp_server/tools.py`) read it to answer questions.

## 3. Human-in-the-loop gate

**When to use.** Two places: when the automation hits a case it should not decide alone, and when an AI agent is about to write to a business system.

**The shape.** For the automation: a review queue with a short closed list of reason codes, each with a documented human action, and a resolve command that records the decision and reruns the one affected event. For the agent: write tools take a `confirm` argument, default false, and return an error and do nothing unless it is exactly `true`. The tool description says "WRITES" first. The client then shows the call with `confirm=true` to the user, who approves it, so the approval is a visible step and not something the model can talk itself into silently.

**Where.** Queue: `hrsync/engine.py:queue_item`, `store.queue_item`, `engine.resolve_with_learner`, reason codes in the [runbook](runbook.md). Agent gate: `mcp_server/tools.py:resolve_queue_item` and `rerun_sync` check `confirm is not True`. Rationale in decisions [0003](decisions/0003-review-queue-not-guessing.md) and [0005](decisions/0005-agent-read-only-by-default.md).

## 4. No personal data in prompts or tool descriptions

**When to use.** Every MCP server or prompt template that touches HR, health or customer data.

**The shape.** Tool descriptions and system prompts describe what a tool does, whether it writes, and what it costs; they never include example names, emails or ids from real records. Personal data reaches the model only as the result of a tool call the user asked for, scoped to one worker id. Keep identifiers opaque (`W-1042`, `L100037`) so a transcript can be shared without a name in it. Test data uses obviously fake names and `@example.com` addresses.

**Where.** The docstrings in `mcp_server/server.py` are the tool descriptions the model sees. The seed generators (`hris/seed.py`, `ethena/seed.py`) build fake people from fixed name lists.

## 5. Client behind an interface, for vendors without a sandbox

**When to use.** The vendor publishes an API spec but gives you no test tenant, which is common for HR and payroll systems.

**The shape.** Define a small Protocol with only the calls your logic needs. Write the real HTTP client against the vendor's published request and response shapes, including pagination and error bodies. Build a mock server that follows the same shapes closely enough that the real client runs against it unchanged. Tests and demos use the real client against the mock; going live means a new base URL and credentials, not new code. Write down every place the mock simplifies the vendor (here: Workday's job-change flow and the `completedFrom` filter).

**Where.** Protocols `HrisClient` and `LearningClient` and the clients `WorkdayClient` and `EthenaClient` in `hrsync/clients.py`; the mocks in `hris/app.py` and `ethena/app.py`; the list of simplifications in [design.md](design.md#what-is-mocked-and-why).
