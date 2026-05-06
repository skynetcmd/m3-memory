"""
M3 Memory — Local-first agentic memory layer for MCP agents.

66 MCP tools · Hybrid search (FTS5 + vector + MMR) · Contradiction detection
Bitemporal history · GDPR Article 17/20 · Cross-device sync · 100% local
"""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("m3-memory")
except PackageNotFoundError:
    __version__ = "0.0.0+local"

__author__ = "skynetcmd, Gemini CLI"
__license__ = "Apache-2.0"
