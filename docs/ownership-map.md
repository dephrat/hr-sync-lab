# Ownership map

Who owns what when this sync runs in production. HR teams own the data and the process in each system. IT owns access and the platform the integration runs on. The automation builder owns the sync logic, the rules it applies, and the documentation that lets the other two groups operate it.

| System | Owning HR team | What IT owns | What the automation builder owns |
|---|---|---|---|
| Workday (HRIS) | HR Technology: job profiles, locations, supervisory orgs, the job-change process | The integration system user and its OAuth credentials, network access, security review, uptime of the integration endpoint | The read-only client (`WorkdayClient`), the watermark, how each job-change reason is handled (Termination deactivates, all others update attributes) |
| Ethena (learning) | Enterprise Enablement: campaigns, their audiences, deadlines, course content | The API key and its rotation, network access, security review | The client (`EthenaClient`), which learner attributes the sync writes, the enrollment check |
| Review queue (`review_queue` in `sync.db`) | HR Operations: works the open items and decides each one | Hosting and backups of `sync.db`, access control on who can run `hrsync resolve` or the MCP write tools | The reason codes and what each one means, the `resolve` command and MCP tools, the [runbook](runbook.md) entry for each code |
| Rules table (`hrsync/rules.default.json`) | HR Technology and Enablement agree on the mapping; Enablement owns which campaigns apply to whom | Change control on the deployed file | The file format, the code that reads it, keeping it in step with campaign audiences in Ethena |
| Sync job and audit log | (consumer: anyone asking "why didn't X get the course?") | The scheduler, logs and alerting on exit code 1, system health | The engine, the audit row format, the reconciliation report, the MCP server, all docs in this folder |

When something goes wrong, the split is: a credential or network failure goes to IT; a queue item goes to HR Operations; a wrong mapping or a missing location in the rules goes to the automation builder, with sign-off from HR Technology or Enablement on the new values.
