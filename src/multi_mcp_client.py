"""MultiMCPClient: manages multiple MCP stdio servers simultaneously.

Each server's tools are namespaced as ``{server_name}__{tool_name}`` so there
are no collisions between e.g. ``snow__get_record`` and ``github__get_file_contents``.
"""
from __future__ import annotations

import json
import os
import select
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ProvisioningError(Exception):
    pass


# ---------------------------------------------------------------------------
# Low-level stdio MCP client (same protocol as azure-mcp-subscription-assistant)
# ---------------------------------------------------------------------------


class StdioMCPClient:
    """Stdio MCP client supporting two wire protocols:

    - ``"lsp"``   — Content-Length framing (used by ``@azure/mcp``)
    - ``"ndjson"`` — newline-delimited JSON (used by servicenow-mcp-server
                    and @modelcontextprotocol/server-github)
    """

    def __init__(
        self,
        command: str,
        env: Optional[Dict[str, str]] = None,
        io_timeout_seconds: float = 60.0,
        protocol: str = "lsp",
    ):
        self.command = command
        self.env = env or {}
        self.io_timeout_seconds = io_timeout_seconds
        self.protocol = protocol  # "lsp" or "ndjson"
        self.proc: Optional[subprocess.Popen[bytes]] = None
        self._next_id = 1

    def __enter__(self) -> "StdioMCPClient":
        argv = shlex.split(self.command)
        if not argv:
            raise ProvisioningError("MCP server command is empty")
        merged_env = os.environ.copy()
        merged_env.update(self.env)
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=merged_env,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def initialize(self):
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "snow-terraform-agent", "version": "0.1.0"},
                "capabilities": {},
            },
        )
        self._notify("notifications/initialized", {})

    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._request("tools/list", {})
        tools = result.get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._request("tools/call", {"name": name, "arguments": arguments})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _notify(self, method: str, params: Dict[str, Any]):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        while True:
            message = self._recv()
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise ProvisioningError(f"MCP {method} error: {json.dumps(message['error'])}")
            result = message.get("result")
            return result if isinstance(result, dict) else {"value": result}

    def _send(self, payload: Dict[str, Any]):
        if not self.proc or not self.proc.stdin:
            raise ProvisioningError("MCP process not started")
        if self.protocol == "ndjson":
            line = json.dumps(payload, ensure_ascii=False) + "\n"
            self.proc.stdin.write(line.encode("utf-8"))
        else:
            body = json.dumps(payload).encode("utf-8")
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            self.proc.stdin.write(header)
            self.proc.stdin.write(body)
        self.proc.stdin.flush()

    def _recv(self) -> Dict[str, Any]:
        if not self.proc or not self.proc.stdout:
            raise ProvisioningError("MCP process not started")

        if self.protocol == "ndjson":
            return self._recv_ndjson()
        return self._recv_lsp()

    def _recv_ndjson(self) -> Dict[str, Any]:
        """Receive one newline-terminated JSON message."""
        self._wait_for_stdout("message")
        line = self.proc.stdout.readline()  # type: ignore[union-attr]
        if not line:
            raise ProvisioningError("MCP (ndjson) process exited unexpectedly")
        text = line.decode("utf-8", errors="ignore").strip()
        if not text:
            # Empty line — try again (some servers send a blank line between messages)
            return self._recv_ndjson()
        return json.loads(text)

    def _recv_lsp(self) -> Dict[str, Any]:
        """Receive one Content-Length framed message (LSP protocol)."""
        headers: Dict[str, str] = {}
        while True:
            self._wait_for_stdout("header")
            line = self.proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                raise ProvisioningError("MCP (lsp) process exited unexpectedly")
            text = line.decode("utf-8", errors="ignore").strip()
            if not text:
                break
            if ":" in text:
                key, value = text.split(":", 1)
                headers[key.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length", "0"))
        if content_length <= 0:
            raise ProvisioningError("Missing Content-Length in MCP response")
        body = self._read_exactly(content_length)
        if not body:
            raise ProvisioningError("Empty MCP response body")
        return json.loads(body.decode("utf-8"))

    def _wait_for_stdout(self, phase: str):
        if not self.proc or not self.proc.stdout:
            raise ProvisioningError("MCP process not started")

        fd = self.proc.stdout.fileno()
        ready, _, _ = select.select([fd], [], [], self.io_timeout_seconds)
        if ready:
            return

        process_state = "running"
        if self.proc.poll() is not None:
            process_state = f"exited ({self.proc.returncode})"

        raise ProvisioningError(
            f"Timed out waiting for MCP {phase} after {self.io_timeout_seconds:.0f}s "
            f"while process is {process_state}."
        )

    def _read_exactly(self, size: int) -> bytes:
        if not self.proc or not self.proc.stdout:
            raise ProvisioningError("MCP process not started")

        chunks: List[bytes] = []
        remaining = size
        started_at = time.monotonic()

        while remaining > 0:
            elapsed = time.monotonic() - started_at
            if elapsed > self.io_timeout_seconds:
                raise ProvisioningError(
                    f"Timed out reading MCP body after {self.io_timeout_seconds:.0f}s"
                )
            self._wait_for_stdout("body")
            piece = self.proc.stdout.read(remaining)
            if not piece:
                raise ProvisioningError("MCP stream ended while reading response body")
            chunks.append(piece)
            remaining -= len(piece)

        return b"".join(chunks)


# ---------------------------------------------------------------------------
# Multi-server client
# ---------------------------------------------------------------------------


@dataclass
class MCPServerConfig:
    name: str
    command: str
    env: Dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0
    protocol: str = "lsp"  # "lsp" (Content-Length) or "ndjson" (newline-delimited)


class MultiMCPClient:
    """Manages multiple MCP stdio servers simultaneously.

    Tool names are namespaced as ``{server_name}__{tool_name}`` to avoid
    collisions.  Use :meth:`all_tools_manifest` to get the combined list and
    :meth:`call_tool` to route a call to the right server.
    """

    def __init__(self, servers: Dict[str, MCPServerConfig]):
        self._configs = servers
        self._clients: Dict[str, StdioMCPClient] = {}
        self._tools_by_server: Dict[str, List[Dict[str, Any]]] = {}

    def __enter__(self) -> "MultiMCPClient":
        started: List[tuple[str, StdioMCPClient]] = []
        try:
            for name, cfg in self._configs.items():
                if not cfg.command.strip():
                    # Silently skip unconfigured servers rather than hard-failing
                    continue
                client = StdioMCPClient(cfg.command, env=cfg.env, io_timeout_seconds=cfg.timeout, protocol=cfg.protocol)
                client.__enter__()
                started.append((name, client))
                client.initialize()
                tools = client.list_tools()
                self._clients[name] = client
                self._tools_by_server[name] = tools
        except Exception:
            for _, c in started:
                try:
                    c.__exit__(None, None, None)
                except Exception:
                    pass
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        for client in self._clients.values():
            try:
                client.__exit__(exc_type, exc, tb)
            except Exception:
                pass

    def all_tools_manifest(self) -> List[Dict[str, Any]]:
        """Return all tools across all servers with ``server__`` prefix on name."""
        result = []
        for server_name, tools in self._tools_by_server.items():
            for tool in tools:
                raw_name = tool.get("name", "")
                prefixed = dict(tool)
                prefixed["name"] = f"{server_name}__{raw_name}"
                result.append(prefixed)
        return result

    def call_tool(self, prefixed_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Route ``server__toolname`` to the right MCP server."""
        server_name, sep, tool_name = prefixed_name.partition("__")
        if not sep:
            raise ProvisioningError(
                f"Tool name must include server prefix (e.g. 'github__create_pull_request'), got: {prefixed_name!r}"
            )
        if server_name not in self._clients:
            raise ProvisioningError(
                f"Unknown MCP server '{server_name}'. Available: {list(self._clients)}"
            )
        return self._clients[server_name].call_tool(tool_name, arguments)


# ---------------------------------------------------------------------------
# Shared helpers used by provisioning_agent
# ---------------------------------------------------------------------------


def format_tool_result(result: Dict[str, Any], max_chars: int = 6000) -> str:
    content = result.get("content")
    if isinstance(content, list):
        chunks: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("text"), str):
                chunks.append(item["text"])
            elif "json" in item:
                chunks.append(json.dumps(item["json"], ensure_ascii=False))
        output = "\n".join(chunks).strip()
    else:
        output = json.dumps(result, ensure_ascii=False)
    return output[:max_chars] + ("\n...[truncated]" if len(output) > max_chars else "")


def tool_manifest_json(tools: List[Dict[str, Any]]) -> str:
    compact = []
    for tool in tools:
        compact.append(
            {
                "name": tool.get("name"),
                "description": tool.get("description", "")[:200],
                "inputSchema": tool.get("inputSchema", {}),
            }
        )
    return json.dumps(compact, ensure_ascii=False)
