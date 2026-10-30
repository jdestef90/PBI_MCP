"""PBIP-503: Container Integration Testing.

Verifies that the MCP server runs successfully inside the Docker container,
responds to client initialization requests over stdio, lists all registered
tools, and correctly executes tool operations against mounted PBIP volumes.
"""

import os
import json
import shutil
import subprocess
import pytest


def _is_docker_available() -> bool:
    try:
        res = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return res.returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _is_docker_available(), reason="Docker daemon not accessible")
def test_docker_container_stdio_protocol():
    workspace = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    cmd = [
        "docker", "run", "--rm", "-i",
        "-v", f"{workspace}:/app/workspace:rw",
        "-e", "PBI_WORKSPACE_PATH=/app/workspace",
        "-e", "LOG_LEVEL=INFO",
        "pbi-mcp-server:latest",
    ]

    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        def send_msg(msg):
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()

        def read_msg(timeout=10):
            line = p.stdout.readline()
            assert line, "Expected JSON-RPC response from container, got EOF"
            return json.loads(line)

        # 1. Initialize
        send_msg({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        })
        init_res = read_msg()
        assert init_res["id"] == 1
        assert init_res["result"]["serverInfo"]["name"] == "pbi-mcp-server"

        # 2. Initialized notification
        send_msg({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # 3. List tools
        send_msg({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools_res = read_msg()
        assert tools_res["id"] == 2
        tool_names = [t["name"] for t in tools_res["result"]["tools"]]
        assert len(tool_names) == 11
        assert "get_model_summary" in tool_names
        assert "rename_measure" in tool_names
        assert "query_report_layout" in tool_names

        # 4. Call tool get_model_summary
        send_msg({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_model_summary", "arguments": {}},
        })
        call_res = read_msg()
        assert call_res["id"] == 3
        assert call_res["result"]["isError"] is False
        summary = call_res["result"]["structuredContent"]
        assert summary["project_name"] == "sample_project"
        assert summary["tables_count"] == 2

        # 5. Call tool list_pages
        send_msg({
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "list_pages", "arguments": {}},
        })
        pages_res = read_msg()
        assert pages_res["id"] == 4
        assert pages_res["result"]["isError"] is False

    finally:
        try:
            if p.stdin and not p.stdin.closed:
                p.stdin.close()
        except Exception:
            pass
        p.kill()
        p.wait(timeout=5)

