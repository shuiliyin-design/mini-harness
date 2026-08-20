#!/usr/bin/env python3
"""Minimal local MCP 2025-11-25 stdio server for teaching and tests."""

import argparse
import json
import os
import sys
import time


PROTOCOL_VERSION = "2025-11-25"
ECHO_TOOL = {
    "name": "echo",
    "description": "回显输入文本",
    "inputSchema": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def error_response(request_id, code, message):
    return {
        "jsonrpc": "2.0", "id": request_id,
        "error": {"code": code, "message": message},
    }


def handle_request(message, state):
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})
    if method == "initialize":
        if params.get("protocolVersion") != PROTOCOL_VERSION:
            return error_response(request_id, -32602, "unsupported protocolVersion")
        state["initialize_seen"] = True
        return {
            "jsonrpc": "2.0", "id": request_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "mini-harness-demo", "version": "9.2",
                    "llmApiKeyVisible": "LLM_API_KEY" in os.environ,
                },
            },
        }
    if method == "notifications/initialized" and "id" not in message:
        if state["initialize_seen"]:
            state["initialized"] = True
        return None
    if not state["initialized"]:
        return error_response(request_id, -32002, "server not initialized")
    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": request_id,
            "result": {"tools": [ECHO_TOOL]},
        }
    if method == "tools/call":
        if params.get("name") != "echo":
            return error_response(request_id, -32601, "unknown tool")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict) or not isinstance(
            arguments.get("text"), str
        ):
            return {
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "text must be a string"}],
                    "isError": True,
                },
            }
        value = {"text": arguments["text"]}
        return {
            "jsonrpc": "2.0", "id": request_id,
            "result": {
                "content": [{
                    "type": "text",
                    "text": json.dumps(value, ensure_ascii=False),
                }],
                "structuredContent": value,
                "isError": False,
            },
        }
    return error_response(request_id, -32601, "method not found")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-mode",
        choices=("crash", "timeout", "malformed-response", "mismatched-id"),
    )
    args = parser.parse_args()
    state = {"initialize_seen": False, "initialized": False}
    for raw_line in sys.stdin:
        try:
            message = json.loads(raw_line)
        except json.JSONDecodeError:
            send(error_response(None, -32700, "parse error"))
            continue
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            send(error_response(message.get("id") if isinstance(message, dict) else None,
                                -32600, "invalid request"))
            continue
        if args.test_mode and message.get("method") == "tools/call":
            if args.test_mode == "crash":
                raise SystemExit(23)
            if args.test_mode == "timeout":
                time.sleep(60)
            if args.test_mode == "malformed-response":
                sys.stdout.write("not-json\n")
                sys.stdout.flush()
                continue
        response = handle_request(message, state)
        if response is not None:
            if args.test_mode == "mismatched-id" and message.get("method") == "tools/call":
                response["id"] = response.get("id", 0) + 100
            send(response)


if __name__ == "__main__":
    main()
