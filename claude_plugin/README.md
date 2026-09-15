# claude_plugin — the `ff-research` Claude Code plugin

Exposes the research agent's ten tools (`research_agent/mcp_server.py`, stdio
MCP) to any local Claude Code session as `mcp__ff-research__*`, plus an
`ff-research` skill that tells the session when and how to use them. No API
key involved — the tools are deterministic fetch/read; the session supplies
the model.

The installable tree is **generated** (it embeds this machine's venv path and
repo path, which stay out of the public repo):

```bash
python3 claude_plugin/build.py          # writes claude_plugin/local/ (gitignored)
# in any Claude Code session:
/plugin marketplace add <repo>/claude_plugin/local
/plugin install ff-research@fantasy-local
```

Already installed on this machine (user scope). After moving the repo or
rebuilding the venv: re-run build.py, then `/plugin marketplace update
fantasy-local`. Verify health with `claude mcp list` → `plugin:ff-research`.
