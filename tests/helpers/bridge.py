"""Shared Bridge integration fakes; never imported by Runtime code."""

import json

from mini_harness_core.providers import FakeProvider


CONSUMER = "codex-proot"


class ScriptedFakeProvider(FakeProvider):
    def __init__(self, decisions):
        self.decisions = iter(decisions)
        self.calls = []

    def complete(self, messages):
        self.calls.append(json.loads(json.dumps(messages)))
        return next(self.decisions)
