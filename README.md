# hr-sync-lab

Two mock HR systems (a Workday-shaped HRIS and an Ethena-shaped learning platform), a sync engine between them, a reconciliation report and an MCP server. All data is fake.

Work in progress.

## Use from Claude Desktop

Start the mock systems (`uv run hrsync seed && uv run hrsync serve`), then add this to
`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "hr-sync-lab": {
      "command": "uv",
      "args": ["run", "--directory", "<repo>", "hrsync-mcp"],
      "env": {
        "HRIS_URL": "http://127.0.0.1:8001",
        "ETHENA_URL": "http://127.0.0.1:8002",
        "ETHENA_API_KEY": "test-key"
      }
    }
  }
}
```

Five tools are read-only; `resolve_queue_item` and `rerun_sync` write and need `confirm=true`.
