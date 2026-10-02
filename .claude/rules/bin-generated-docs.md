---
paths:
  - "bin/**/*.py"
---

# Editing anything under `bin/`

Run `python bin/gen_tool_inventory.py` **in the same change**.

Each `bin/` script has a generated page under `docs/tools/` stamped with a hash
of its source, and `tests/test_generated_docs_fresh.py::test_tool_pages_are_fresh`
fails when the source moves without the page.

This is a THIRD generator, separate from `gen_tool_manifest.py` and
`gen_mcp_inventory.py` (which cover the MCP catalog). Neither the pre-push hook
nor `check_tool_catalog_drift.py` runs it — only the full suite catches it, at
the end of a ~5.5-minute run. Regenerate right after the edit, not after the
failure. (Hit three times in one session, 2026-09-08.)
