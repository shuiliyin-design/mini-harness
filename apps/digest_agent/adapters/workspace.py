"""Fixed workspace Artifact materialization/observation MCP adapter."""

import hashlib
import os
import tempfile

from mini_harness_core.mcp import MCPClient


class WorkspaceArtifactClient(MCPClient):
    """Write only prevalidated payload identities inside one workspace."""

    def __init__(self, workspace, payloads=None):
        self.workspace = os.path.realpath(workspace)
        self.payloads = dict(payloads or {})
        self.calls = []

    def register(self, payload):
        identity = hashlib.sha256(payload).hexdigest()
        self.payloads[identity] = bytes(payload)
        return identity

    def list_tools(self):
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"], "additionalProperties": False,
        }
        return [{
            "name": "materialize",
            "description": "Materialize one prevalidated Digest payload",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "payload_sha256": {"type": "string"},
                },
                "required": ["path", "payload_sha256"],
                "additionalProperties": False,
            },
        }, {
            "name": "observe",
            "description": "Observe one Digest Artifact identity",
            "inputSchema": schema,
        }]

    def _path(self, relative):
        if (not isinstance(relative, str) or not relative
                or os.path.isabs(relative) or "\\" in relative
                or os.path.normpath(relative).replace(os.sep, "/") != relative
                or ".." in relative.split("/")):
            raise ValueError("workspace Artifact path 无效")
        path = os.path.realpath(os.path.join(self.workspace, relative))
        if os.path.commonpath((self.workspace, path)) != self.workspace:
            raise ValueError("workspace Artifact path 逃逸")
        return path

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        path = self._path(arguments["path"])
        if name == "materialize":
            identity = arguments["payload_sha256"]
            payload = self.payloads.get(identity)
            if payload is None or hashlib.sha256(payload).hexdigest() != identity:
                raise ValueError("unknown prevalidated payload identity")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=".digest-", suffix=".tmp", dir=os.path.dirname(path),
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        elif name != "observe":
            raise ValueError("workspace Artifact tool 不存在")
        with open(path, "rb") as stream:
            current = stream.read()
        return {
            "path": arguments["path"],
            "sha256": hashlib.sha256(current).hexdigest(),
            "size": len(current),
        }
