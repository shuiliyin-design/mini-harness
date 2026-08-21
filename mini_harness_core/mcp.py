"""MCP clients, registry, schema validation, and stdio transport."""

import json
import os
import re
import select
import subprocess
import sys
import queue
import threading

from .policy_composition import (
    ALLOW as COMPOSE_ALLOW, EXTERNAL, GLOBAL_SECURITY_POLICY,
    SIDE_EFFECTING, CapabilityProfile, StaticPolicyLayer,
    ZONE_POLICIES, compose_static_policy, local_mcp_mapping,
)


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_ALLOW = "ALLOW"
POLICY_ASK = "ASK"
POLICY_DENY = "DENY"
MCP_TOOL_REFERENCE = re.compile(
    r"^mcp:([a-zA-Z0-9][a-zA-Z0-9_.-]*):([a-zA-Z0-9][a-zA-Z0-9_.-]*)$"
)
MCP_EFFECT_READ_ONLY = "read_only"
MCP_EFFECT_SIDE_EFFECTING = "side_effecting"
MCP_EFFECT_UNKNOWN = "unknown"
MCP_PROTOCOL_VERSION = "2025-11-25"
MCP_DEFAULT_TIMEOUT = 2.0


def _policy_result(action, reason):
    return {"action": action, "reason": reason}


class MCPClient:
    """Transport abstraction only; it owns neither model decisions nor authority."""

    def list_tools(self):
        raise NotImplementedError

    def call_tool(self, name, arguments):
        raise NotImplementedError

    def list_resources(self):
        """Resources stay a separate, read-only discovery surface in V9."""
        return []

    def read_resource(self, uri):
        raise NotImplementedError

    def close(self):
        """Release transport resources. In-process teaching fakes need no work."""


class MCPError(RuntimeError):
    """A stdio transport, protocol, or remote MCP failure."""


class StdioMCPClient(MCPClient):
    """Sequential MCP 2025-11-25 client over a persistent child process."""

    ENV_ALLOWLIST = frozenset({
        "PATH", "PYTHONPATH", "PYTHONHOME", "LANG", "LC_ALL", "LC_CTYPE",
        "SYSTEMROOT", "WINDIR", "TMPDIR", "TEMP", "TMP",
    })

    def __init__(self, command=None, timeout=MCP_DEFAULT_TIMEOUT):
        self.command = list(command or [
            sys.executable, os.path.join(PROJECT_ROOT, "mcp_demo_server.py")
        ])
        self.timeout = timeout
        self.process = None
        self._next_id = 1
        self.initialized = False
        self.server_info = None

    @classmethod
    def isolated_environment(cls):
        """Copy only runtime essentials; Harness/API secrets are never inherited."""
        return {
            name: value for name, value in os.environ.items()
            if name in cls.ENV_ALLOWLIST
        }

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        try:
            self.process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.isolated_environment(),
                cwd=PROJECT_ROOT,
            )
            self.initialized = False
            self.initialize()
        except Exception:
            self.close()
            raise

    def _write(self, message):
        if self.process is None or self.process.poll() is not None:
            code = None if self.process is None else self.process.returncode
            raise MCPError(f"MCP server 未运行（exit_code={code}）")
        try:
            payload = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise MCPError(f"MCP server stdin 写入失败：{error}") from error

    def _read_response(self, request_id, method):
        ready, _, _ = select.select(
            [self.process.stdout], [], [], self.timeout
        )
        if not ready:
            raise MCPError(f"MCP request timeout：{method}")
        raw = self.process.stdout.readline()
        if not raw:
            code = self.process.poll()
            raise MCPError(f"MCP server EOF（exit_code={code}）")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MCPError("MCP response 不是合法 JSON") from error
        if not isinstance(response, dict) or response.get("jsonrpc") != "2.0":
            raise MCPError("MCP response JSON-RPC 格式无效")
        if response.get("id") != request_id:
            raise MCPError(
                f"MCP response id 不匹配：expected={request_id!r}, "
                f"actual={response.get('id')!r}"
            )
        if "error" in response:
            error = response["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise MCPError(f"MCP JSON-RPC error：{message}")
        if not isinstance(response.get("result"), dict):
            raise MCPError("MCP response 缺少 result object")
        return response["result"]

    def _request(self, method, params=None):
        if self.process is None or self.process.poll() is not None:
            raise MCPError("MCP server 未运行")
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        try:
            return self._read_response(request_id, method)
        except MCPError:
            # Correlation is no longer trustworthy after a transport/protocol error.
            self.close()
            raise

    def initialize(self):
        if self.process is None:
            raise MCPError("MCP server 尚未启动")
        result = self._request("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mini-harness", "version": "9.2"},
        })
        if result.get("protocolVersion") != MCP_PROTOCOL_VERSION:
            raise MCPError("MCP protocolVersion 不匹配")
        self.server_info = result.get("serverInfo")
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.initialized = True
        return result

    def list_tools(self):
        if not self.initialized:
            self.start()
        result = self._request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise MCPError("MCP tools/list 缺少 tools array")
        return tools

    def call_tool(self, name, arguments):
        if not self.initialized:
            self.start()
        result = self._request("tools/call", {
            "name": name, "arguments": arguments,
        })
        if result.get("isError") is True:
            content = result.get("content", [])
            message = content[0].get("text") if content and isinstance(
                content[0], dict
            ) else "MCP tool 调用失败"
            raise MCPError(message)
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        raise MCPError("MCP tool result 缺少 structuredContent")

    def close(self):
        process, self.process = self.process, None
        self.initialized = False
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdout, process.stderr):
            if stream:
                stream.close()


class FakeMCPClient(MCPClient):
    """Deterministic, offline MCP server used by the V9 teaching loop."""

    def __init__(self):
        self.tool_calls = []

    def list_tools(self):
        return [{
            "name": "echo",
            "description": "回显输入文本",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }]

    def call_tool(self, name, arguments):
        if name != "echo":
            raise ValueError("MCP tool 不存在")
        self.tool_calls.append((name, dict(arguments)))
        return {"text": arguments["text"]}


class MCPRegistry:
    """Harness-owned MCP discovery, schema lookup and local policy configuration."""

    def __init__(self, clients, tool_policies=None, tool_effects=None):
        self.clients = dict(clients)
        self.tool_policies = dict(tool_policies or {})
        self.tool_effects = dict(tool_effects or {})
        self._catalog = None
        self._details = {}

    def capability_catalog(self):
        """Fetch compact metadata once; never put full schemas in model context."""
        if self._catalog is None:
            catalog = []
            for server, client in sorted(self.clients.items()):
                for tool in client.list_tools():
                    name = tool.get("name")
                    description = tool.get("description")
                    if (
                        not isinstance(name, str)
                        or not isinstance(tool.get("inputSchema"), dict)
                        or not MCP_TOOL_REFERENCE.fullmatch(f"mcp:{server}:{name}")
                    ):
                        continue
                    catalog.append({
                        "tool": f"mcp:{server}:{name}",
                        "description": description if isinstance(description, str) else "",
                        "input": self._compact_input(tool["inputSchema"]),
                    })
                    # Standard tools/list already carries inputSchema. Keep the
                    # full definition in Harness runtime state, not model context.
                    self._details[f"mcp:{server}:{name}"] = dict(tool)
            self._catalog = catalog
        return [dict(item) for item in self._catalog]

    @staticmethod
    def _compact_input(schema):
        """Expose only top-level argument names/types required for tool selection."""
        if schema.get("type") != "object" or not isinstance(
            schema.get("properties", {}), dict
        ):
            return {"type": schema.get("type", "unknown")}
        required = set(schema.get("required", []))
        return {
            name: {
                "type": value.get("type", "unknown"),
                "required": name in required,
            }
            for name, value in schema.get("properties", {}).items()
            if isinstance(value, dict)
        }

    def resolve(self, reference):
        match = MCP_TOOL_REFERENCE.fullmatch(reference or "")
        if not match:
            raise ValueError("MCP tool reference 格式无效")
        server, name = match.groups()
        client = self.clients.get(server)
        if client is None:
            raise ValueError("MCP server 不存在")
        if reference not in {item["tool"] for item in self.capability_catalog()}:
            raise ValueError("MCP tool 不存在")
        detail = self._details.get(reference)
        if not isinstance(detail, dict) or detail.get("name") != name:
            raise ValueError("MCP tool detail 无效")
        return client, name, detail

    def policy_for(self, reference):
        """Policy is local Harness configuration; server metadata is ignored."""
        action = self.tool_policies.get(reference, POLICY_ASK)
        if action not in {POLICY_ALLOW, POLICY_ASK, POLICY_DENY}:
            action = POLICY_DENY
        global_layer = StaticPolicyLayer(
            "global", action, GLOBAL_SECURITY_POLICY.allowed_tools,
            GLOBAL_SECURITY_POLICY.max_effect,
            GLOBAL_SECURITY_POLICY.can_write_workspace,
            GLOBAL_SECURITY_POLICY.can_use_mcp,
        )
        # Preserve V8's explicit local MCP policy.  Effect/durability remain a
        # later classification/runtime concern; the static external ceiling
        # does not infer authority from server metadata.
        profile = CapabilityProfile(
            "local-mcp-capability", COMPOSE_ALLOW, frozenset({"mcp"}),
            SIDE_EFFECTING, False, True,
        )
        effective = compose_static_policy(
            global_layer, ZONE_POLICIES[EXTERNAL], profile,
        )
        return {
            "action": effective.policy,
            "reason": "Harness 本地 MCP tool policy",
            "trace": effective.trace,
        }

    def effect_for(self, reference):
        """Effect is trusted only when it comes from local Harness configuration."""
        effect = self.tool_effects.get(reference, MCP_EFFECT_UNKNOWN)
        if effect not in {
            MCP_EFFECT_READ_ONLY,
            MCP_EFFECT_SIDE_EFFECTING,
            MCP_EFFECT_UNKNOWN,
        }:
            return MCP_EFFECT_UNKNOWN
        return effect

    def capability_mapping(self, reference):
        """Return only Harness-local authority facts, never server metadata."""
        return local_mcp_mapping(reference, self.effect_for(reference))

    def close(self):
        for client in self.clients.values():
            client.close()


def validate_json_schema(value, schema, path="arguments"):
    """Validate a small, explicit JSON Schema subset for the teaching harness."""
    if not isinstance(schema, dict):
        raise ValueError("MCP input schema 无效")
    schema_type = schema.get("type")
    type_checks = {
        "object": dict, "array": list, "string": str,
        "number": (int, float), "integer": int, "boolean": bool, "null": type(None),
    }
    if schema_type in type_checks:
        expected = type_checks[schema_type]
        if not isinstance(value, expected) or (
            schema_type in {"number", "integer"} and isinstance(value, bool)
        ):
            raise ValueError(f"{path} 必须是 {schema_type}")
    elif schema_type is not None:
        raise ValueError(f"不支持的 MCP schema type：{schema_type}")

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} 不在 enum 中")
    if schema_type == "object":
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if not isinstance(properties, dict) or not isinstance(required, list):
            raise ValueError("MCP object schema 无效")
        for name in required:
            if name not in value:
                raise ValueError(f"{path}.{name} 是必填字段")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                raise ValueError(f"{path} 包含未知字段：{sorted(unknown)[0]}")
        for name, item in value.items():
            if name in properties:
                validate_json_schema(item, properties[name], f"{path}.{name}")
    elif schema_type == "array" and "items" in schema:
        for index, item in enumerate(value):
            validate_json_schema(item, schema["items"], f"{path}[{index}]")


def execute_mcp_tool(registry, reference, arguments, timeout=None):
    """Call failures are ordinary Observations, not Agent failures."""
    try:
        client, name, detail = registry.resolve(reference)
        schema = detail.get("inputSchema", {"type": "object"})
        validate_json_schema(arguments, schema)
        original_timeout = getattr(client, "timeout", None)
        if timeout is not None and original_timeout is not None:
            client.timeout = min(original_timeout, timeout)
        try:
            if timeout is None:
                result = client.call_tool(name, arguments)
            else:
                completed = queue.Queue(maxsize=1)

                def call():
                    try:
                        completed.put((True, client.call_tool(name, arguments)))
                    except BaseException as error:
                        completed.put((False, error))

                thread = threading.Thread(target=call, daemon=True)
                thread.start()
                try:
                    succeeded, value = completed.get(timeout=timeout)
                except queue.Empty as error:
                    raise MCPError("MCP request timeout：tools/call") from error
                if not succeeded:
                    raise value
                result = value
        finally:
            if timeout is not None and original_timeout is not None:
                client.timeout = original_timeout
        return {
            "result": result,
            "error": None,
            "exit_code": 0,
            "source": reference,
            "trust": "untrusted external observation",
        }
    except Exception as error:
        timed_out = "timeout" in str(error).lower() or "timed out" in str(error).lower()
        return {
            "result": None,
            "error": str(error),
            "exit_code": -1 if timed_out else 1,
            "status": "timeout" if timed_out else "failed",
            "source": reference,
            "trust": "untrusted external observation",
        }


# ==================== Model / Provider ====================
