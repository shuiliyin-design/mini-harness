"""Session persistence and resume support."""

import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone

from .planning import validate_plan, validate_revision_history
from .durability import validate_action_checkpoint
from .run_control import create_run_control, validate_run_control


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SESSIONS_DIR = os.path.join(PROJECT_ROOT, ".sessions")
SESSION_VERSION = 4
DURABILITY_SESSION_VERSION = 3
PLAN_SESSION_VERSION = 2
LEGACY_SESSION_VERSION = 1
SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def utc_now():
    """Return a stable, JSON-friendly UTC timestamp."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SessionStore:
    """Persist explicit Agent sessions as small, atomic JSON files."""

    def __init__(self, directory=SESSIONS_DIR):
        self.directory = directory

    def _path(self, session_id):
        if not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("无效的 session_id（应为 32 位小写十六进制字符串）")
        return os.path.join(self.directory, f"{session_id}.json")

    def create(self):
        now = utc_now()
        session = {
            "version": SESSION_VERSION,
            "session_id": uuid.uuid4().hex,
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "verification": {
                "requires_verification": False,
                "verification_target": None,
                "latest_write_command": None,
            },
            "current_plan": None,
            "plan_revision_history": [],
            "current_action_checkpoint": None,
            "run_control": create_run_control(now),
        }
        self.save(session)
        return session

    def load(self, session_id):
        path = self._path(session_id)
        try:
            with open(path, encoding="utf-8") as session_file:
                session = json.load(session_file)
        except FileNotFoundError as error:
            raise ValueError(f"session 不存在：{session_id}") from error
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"无法读取 session：{error}") from error
        self._validate(session, expected_id=session_id)
        if session["version"] == LEGACY_SESSION_VERSION:
            session["version"] = SESSION_VERSION
            session["current_plan"] = None
            session["plan_revision_history"] = []
            session["current_action_checkpoint"] = None
            session["run_control"] = create_run_control()
        elif session["version"] == PLAN_SESSION_VERSION:
            session["version"] = SESSION_VERSION
            session["current_action_checkpoint"] = None
            session["run_control"] = create_run_control()
        elif session["version"] == DURABILITY_SESSION_VERSION:
            session["version"] = SESSION_VERSION
            session["run_control"] = create_run_control()
        return session

    def save(self, session):
        self._validate(session)
        os.makedirs(self.directory, exist_ok=True)
        session["updated_at"] = utc_now()
        path = self._path(session["session_id"])
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{session['session_id']}.", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as session_file:
                json.dump(session, session_file, ensure_ascii=False, indent=2)
                session_file.write("\n")
                session_file.flush()
                os.fsync(session_file.fileno())
            os.replace(temporary_path, path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise

    @staticmethod
    def _validate(session, expected_id=None):
        if not isinstance(session, dict):
            raise ValueError("session JSON 必须是对象")
        version = session.get("version")
        if version not in {
            LEGACY_SESSION_VERSION, PLAN_SESSION_VERSION,
            DURABILITY_SESSION_VERSION, SESSION_VERSION,
        }:
            raise ValueError(f"不支持的 session version：{session.get('version')!r}")
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            raise ValueError("session JSON 中的 session_id 无效")
        if expected_id is not None and session_id != expected_id:
            raise ValueError("session 文件名与内容中的 session_id 不一致")
        if not isinstance(session.get("created_at"), str):
            raise ValueError("session 缺少 created_at")
        if not isinstance(session.get("updated_at"), str):
            raise ValueError("session 缺少 updated_at")
        messages = session.get("messages")
        if not isinstance(messages, list) or not all(
            isinstance(message, dict)
            and message.get("role") in {"user", "assistant", "tool"}
            and isinstance(message.get("content"), str)
            for message in messages
        ):
            raise ValueError("session messages 格式无效")
        verification = session.get("verification")
        if not isinstance(verification, dict):
            raise ValueError("session verification 格式无效")
        if not isinstance(verification.get("requires_verification"), bool):
            raise ValueError("session verification 状态无效")
        if version == LEGACY_SESSION_VERSION:
            if "current_plan" in session or "plan_revision_history" in session:
                raise ValueError("legacy session 不应包含 V12 plan 字段")
            return
        if version == PLAN_SESSION_VERSION:
            if set(session) != {
                "version", "session_id", "created_at", "updated_at", "messages",
                "verification", "current_plan", "plan_revision_history",
            }:
                raise ValueError("session schema 无效")
            current_plan = session["current_plan"]
            if current_plan is not None:
                validate_plan(current_plan)
            validate_revision_history(session["plan_revision_history"])
            return
        expected_fields = {
            "version", "session_id", "created_at", "updated_at", "messages",
            "verification", "current_plan", "plan_revision_history",
            "current_action_checkpoint",
        }
        if version == SESSION_VERSION:
            expected_fields.add("run_control")
        if set(session) != expected_fields:
            raise ValueError("session schema 无效")
        current_plan = session["current_plan"]
        if current_plan is not None:
            validate_plan(current_plan)
        validate_revision_history(session["plan_revision_history"])
        if session["current_action_checkpoint"] is not None:
            validate_action_checkpoint(session["current_action_checkpoint"])
        if version == SESSION_VERSION:
            validate_run_control(session["run_control"])
