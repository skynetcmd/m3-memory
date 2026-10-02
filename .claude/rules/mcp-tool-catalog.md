---
paths:
  - "bin/mcp_tool_catalog.py"
  - "bin/mcp_catalog/**/*.py"
  - "bin/tool_domains.py"
---

# Editing the MCP tool catalog

Regenerate the catalog **and** the inventory, and update the "N tools" counts in
the same change. `python bin/check_tool_catalog_drift.py` must pass — it is
enforced by the local pre-push hook AND by CI, not just convention.

Plugin manifest descriptions say "100+ MCP tools", never an exact count. That is
also test-guarded: an exact number there drifts the moment a tool is added.
