#!/usr/bin/env python3
"""
test_mcp_proxy_unit.py - In-process unit tests for mcp_proxy.

Unlike test_mcp_proxy.py (which exercises the running HTTP proxy with real
provider keys), this suite imports mcp_proxy as a module and verifies:
  - It imports without ImportError (regression for the m3_sdk break)
  - Tool merging from PROTOCOL + DEBUG + catalog produces the expected count
  - Default-allow filtering hides destructive catalog tools
  - MCP_PROXY_ALLOW_DESTRUCTIVE=1 exposes them
  - _execute_tool dispatches catalog tools through mcp_tool_catalog.execute_tool
  - _execute_tool refuses unknown tools and gives a helpful error for disabled destructive tools
  - inject_agent_id is honored on memory_write
"""
import asyncio
import os
import sys
import unittest
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))


class TestMcpProxyImportAndCatalog(unittest.TestCase):
    """Default mode: ALLOW_DESTRUCTIVE=False."""

    @classmethod
    def setUpClass(cls):
        os.environ.pop("MCP_PROXY_ALLOW_DESTRUCTIVE", None)
        if "mcp_proxy" in sys.modules:
            del sys.modules["mcp_proxy"]
        import mcp_proxy
        cls.mcp_proxy = mcp_proxy

    def test_imports_clean(self):
        self.assertTrue(hasattr(self.mcp_proxy, "PROXY_HOST"))
        self.assertTrue(hasattr(self.mcp_proxy, "LMSTUDIO_BASE"))
        self.assertEqual(self.mcp_proxy.PROXY_PORT, 9000)

    def test_protocol_tool_count(self):
        self.assertEqual(len(self.mcp_proxy.PROTOCOL_TOOLS), 5)
        names = {t["function"]["name"] for t in self.mcp_proxy.PROTOCOL_TOOLS}
        expected = {"log_activity", "query_decisions", "update_focus",
                    "retire_focus", "check_thermal_load"}
        self.assertEqual(names, expected)

    def test_debug_tool_count(self):
        self.assertEqual(len(self.mcp_proxy.DEBUG_TOOLS), 6)
        names = {t["function"]["name"] for t in self.mcp_proxy.DEBUG_TOOLS}
        expected = {"debug_analyze", "debug_bisect", "debug_trace",
                    "debug_correlate", "debug_history", "debug_report"}
        self.assertEqual(names, expected)

    def test_catalog_default_filters_destructive(self):
        self.assertFalse(self.mcp_proxy.ALLOW_DESTRUCTIVE)
        catalog_schemas, specs = self.mcp_proxy._build_catalog_tools()
        catalog_names = {t["function"]["name"] for t in catalog_schemas}
        destructive = {
            "memory_delete", "chroma_sync", "memory_maintenance",
            "memory_set_retention", "memory_export", "memory_import",
            "gdpr_export", "gdpr_forget", "agent_offline",
        }
        leaked = catalog_names & destructive
        self.assertEqual(leaked, set(),
                         f"Destructive tools leaked into default mode: {leaked}")

    def test_total_tool_count_default(self):
        catalog_schemas, _ = self.mcp_proxy._build_catalog_tools()
        total = (len(self.mcp_proxy.PROTOCOL_TOOLS)
                 + len(self.mcp_proxy.DEBUG_TOOLS)
                 + len(catalog_schemas))
        self.assertEqual(total, len(self.mcp_proxy.get_mcp_tools()))
        self.assertGreaterEqual(total, 40)

    def test_execute_unknown_tool(self):
        result = asyncio.run(
            self.mcp_proxy._execute_tool("nope_not_real", {}, "tester")
        )
        self.assertIn("Unknown MCP tool", result)

    def test_execute_destructive_tool_default_denied(self):
        result = asyncio.run(
            self.mcp_proxy._execute_tool("memory_delete", {"id": "x"}, "tester")
        )
        self.assertIn("destructive and disabled by default", result)

    def test_execute_catalog_tool_agent_list(self):
        # agent_list is non-destructive and should always work
        result = asyncio.run(
            self.mcp_proxy._execute_tool("agent_list", {}, "test-smoke")
        )
        self.assertIsInstance(result, str)
        # Either "Agents (" or "No agents" depending on DB state
        self.assertTrue(
            result.startswith("Agents (") or "agents" in result.lower(),
            f"Unexpected agent_list output: {result[:200]}"
        )

    def test_inject_agent_id_on_memory_write(self):
        import mcp_tool_catalog as cat
        spec = cat.get_tool("memory_write")
        self.assertIsNotNone(spec)
        self.assertTrue(spec.inject_agent_id,
                        "memory_write must have inject_agent_id=True")

    def test_legacy_dispatch_table_complete(self):
        # Every name in PROTOCOL_TOOLS + DEBUG_TOOLS must have a dispatch entry
        legacy_names = (
            {t["function"]["name"] for t in self.mcp_proxy.PROTOCOL_TOOLS}
            | {t["function"]["name"] for t in self.mcp_proxy.DEBUG_TOOLS}
        )
        dispatch_keys = set(self.mcp_proxy._LEGACY_DISPATCH.keys())
        missing = legacy_names - dispatch_keys
        self.assertEqual(missing, set(),
                         f"Legacy tools missing from _LEGACY_DISPATCH: {missing}")


class TestMcpProxyAllowDestructive(unittest.TestCase):
    """ALLOW_DESTRUCTIVE=1 mode."""

    @classmethod
    def setUpClass(cls):
        os.environ["MCP_PROXY_ALLOW_DESTRUCTIVE"] = "1"
        if "mcp_proxy" in sys.modules:
            del sys.modules["mcp_proxy"]
        import mcp_proxy
        cls.mcp_proxy = mcp_proxy

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("MCP_PROXY_ALLOW_DESTRUCTIVE", None)
        if "mcp_proxy" in sys.modules:
            del sys.modules["mcp_proxy"]

    def test_destructive_exposed(self):
        self.assertTrue(self.mcp_proxy.ALLOW_DESTRUCTIVE)
        catalog_schemas, _ = self.mcp_proxy._build_catalog_tools()
        catalog_names = {t["function"]["name"] for t in catalog_schemas}
        destructive = {
            "memory_delete", "chroma_sync", "memory_maintenance",
            "memory_set_retention", "memory_export", "memory_import",
            "gdpr_export", "gdpr_forget", "agent_offline",
        }
        present = catalog_names & destructive
        self.assertEqual(present, destructive,
                         f"Missing destructive tools when enabled: {destructive - present}")

    def test_full_catalog_count(self):
        # 44 catalog tools when destructive enabled
        catalog_schemas, _ = self.mcp_proxy._build_catalog_tools()
        self.assertEqual(len(catalog_schemas), 44)


if __name__ == "__main__":
    unittest.main(verbosity=2)
