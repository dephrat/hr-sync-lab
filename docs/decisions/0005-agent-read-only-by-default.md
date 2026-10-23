# 0005: The MCP server is read-only unless the caller confirms

**Context.** The MCP server lets an assistant answer questions like "why isn't Priya enrolled in Manager Fundamentals?". The same server can resolve queue items and run the sync, which write to Ethena. A model can call a tool because of a misread question or text it was shown.

**Decision.** Five tools only read: `get_worker`, `get_learner_status`, `get_audit_trail`, `list_queue`, `explain_gap`. Two write: `resolve_queue_item` and `rerun_sync`. Their descriptions start with "WRITES" and they return `{"error": "confirm required"}` and change nothing unless called with `confirm=true`. Tool descriptions name the cost in HTTP calls and contain no personal data.

**Consequences.** The flag forces each write to be a separate, explicit tool call whose arguments show the intent. A person approves that call in the client's own tool-approval prompt, not through the flag: the model fills in `confirm=true` itself, so the flag is not proof of human approval. Where approval must be guaranteed, set the client to always ask before these two tools, or drop them. Writes made through MCP go through the same engine functions as the CLI and land in the same audit log (`resolve_queue_item` records the note "via mcp"). A stricter deployment can drop the two write tools entirely.
