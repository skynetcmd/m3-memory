"""The sandbox's Node base image and OpenClaw pin are ONE constraint.

`openclaw@2026.9.4` declares `engines.node: ">=24.16.0 <25 || >=26.1.0"`. Running
OpenClaw on an under-version Node does not fail cleanly — upstream's 2026.9.3
notes report SQLite text truncation, i.e. silent data corruption. So bumping the
npm pin without the `FROM node:` line (or vice versa) ships a broken image.

Today that coupling is only a comment in the Dockerfile, which is exactly the
shape §3 calls out: a declared limit with no enforcement site is a lie the tests
cannot see. These tests are the enforcement site.

Deliberately OFFLINE: the floor is asserted against a constant recorded here, not
fetched from the network, so the suite stays hermetic (a test that needs the
internet fails in CI). When you bump the pin, re-read upstream's engines.node and
update _EXPECTED_NODE_FLOOR in the same commit.
"""
from __future__ import annotations

import json
import os
import re

import pytest

_SANDBOX = os.path.join(os.path.dirname(__file__), "..", "examples", "sandbox-openclaw")
_DOCKERFILE = os.path.join(_SANDBOX, "Dockerfile")
_CONFIG = os.path.join(_SANDBOX, "config", "openclaw.json")

# Verified against github.com/openclaw/openclaw package.json at v2026.9.4.
_EXPECTED_NODE_FLOOR = 24
# The release that introduced the native MCP client (bisected: 2026.3.11 has no
# src/config/mcp-config.ts, 2026.3.22 does). The config below needs at least this.
_MCP_FLOOR = (2026, 3, 22)


def _dockerfile() -> str:
    with open(_DOCKERFILE, encoding="utf-8") as fh:
        return fh.read()


def _pinned_openclaw() -> tuple[int, ...]:
    m = re.search(r"npm install -g openclaw@(\d+(?:\.\d+)+)", _dockerfile())
    assert m, "could not find the openclaw npm pin in the Dockerfile"
    return tuple(int(x) for x in m.group(1).split("."))


def _base_node_major() -> int:
    m = re.search(r"^FROM node:(\d+)", _dockerfile(), flags=re.MULTILINE)
    assert m, "could not find a `FROM node:<major>` line in the Dockerfile"
    return int(m.group(1))


def test_base_image_satisfies_the_openclaw_node_floor():
    """The whole point: the two pins must not drift apart."""
    assert _base_node_major() >= _EXPECTED_NODE_FLOOR, (
        f"FROM node:{_base_node_major()}-slim is below the Node "
        f"{_EXPECTED_NODE_FLOOR} floor that openclaw@"
        f"{'.'.join(map(str, _pinned_openclaw()))} requires. An under-version Node "
        f"causes SQLite text truncation, not a clean failure."
    )


def test_pinned_openclaw_has_native_mcp():
    """The config/openclaw.json `mcp` block is dead weight below 2026.3.22."""
    assert _pinned_openclaw() >= _MCP_FLOOR, (
        f"openclaw@{'.'.join(map(str, _pinned_openclaw()))} predates native MCP "
        f"({'.'.join(map(str, _MCP_FLOOR))}); the mcp section would be ignored."
    )


def test_openclaw_pin_is_exact_not_a_range():
    """An example image should be reproducible; @latest silently drifts."""
    df = _dockerfile()
    assert not re.search(r"openclaw@(latest|\^|~|\*)", df), (
        "the sandbox pins openclaw to an exact version on purpose — a floating "
        "tag makes the image non-reproducible and can cross the Node floor "
        "without any file in this repo changing."
    )


# ── the config the pins exist to support ─────────────────────────────────────

def _server() -> dict:
    with open(_CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    # Shape confirmed against the live CLI: `openclaw mcp set` writes to
    # mcp.servers.<name>, not mcp.<name>.
    return cfg["mcp"]["servers"]["m3_memory"]


def test_sandbox_uses_http_not_stdio():
    """stdio cannot work here: the image has no m3 and no mounted bridge."""
    s = _server()
    assert s["transport"] == "streamable-http"
    assert "command" not in s, (
        "a stdio `command` in the sandbox would point at a host interpreter path "
        "that does not exist inside the container"
    )
    assert s["url"].endswith("/mcp")


def test_sandbox_token_is_a_placeholder_never_a_literal():
    """A real bearer token must never be committed in an example."""
    auth = _server()["headers"]["Authorization"]
    assert auth.startswith("Bearer ${"), f"token is not interpolated: {auth!r}"
    assert "M3_SERVE_TOKEN" in auth


def test_sandbox_tool_filter_matches_the_bridge_startup_set():
    """Keeps the example's filter honest against the real startup surface."""
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
    try:
        import mcp_tool_catalog
        import memory_bridge
        import tool_domains
    except Exception as e:  # pragma: no cover — payload must be importable
        pytest.skip(f"payload not importable: {e}")

    meta = set(memory_bridge._META_TOOLS)
    expected = sorted({
        s.name for s in mcp_tool_catalog.TOOLS
        if s.name in meta or tool_domains.is_essential(s.name)
    })
    assert sorted(_server()["toolFilter"]["include"]) == expected


def test_compose_makes_host_docker_internal_resolvable():
    """host.docker.internal is Docker-Desktop-only without host-gateway.

    §0.4: a green run on Windows proves nothing about stock Linux Docker.
    """
    with open(os.path.join(_SANDBOX, "docker-compose.yml"), encoding="utf-8") as fh:
        compose = fh.read()
    assert "host.docker.internal" in _server()["url"]
    assert "host-gateway" in compose, (
        "config/openclaw.json dials host.docker.internal, which does not resolve "
        "on stock Linux Docker without extra_hosts: host-gateway"
    )
