"""Deterministic offline Search adapter; observations are not Evidence."""

import copy

from mini_harness_core.mcp import MCPClient


class FakeSearchClient(MCPClient):
    """MCP-shaped Fake Search with fixed, injectable result fixtures."""

    def __init__(self, results):
        self.results = tuple(copy.deepcopy(tuple(results)))
        self.calls = []

    def list_tools(self):
        return [{
            "name": "web_search",
            "description": "Offline deterministic web search fixture",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
                "required": ["query", "max_results"],
                "additionalProperties": False,
            },
        }]

    def call_tool(self, name, arguments):
        if name != "web_search":
            raise ValueError("Fake Search tool 不存在")
        self.calls.append(copy.deepcopy(arguments))
        maximum = arguments["max_results"]
        return {"results": copy.deepcopy(list(self.results[:maximum]))}
