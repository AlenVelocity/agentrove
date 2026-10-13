from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.services.acp.adapters import AgentKind
from app.services.exceptions import AuthException
from app.services.ordin_cloud import OrdinCloudService, ordin_cloud_service, ordin_error

logger = logging.getLogger(__name__)

# The one pseudo workspace every Ordin chat lives in, so the chat page and the
# landing page have a workspace id to hang on.
ORDIN_WORKSPACE_ID = "00000000-0000-4000-8000-00000000ad1e"
ORDIN_WORKSPACE_NAME = "Ordin (cloud)"
MODELS_CACHE_SECONDS = 3600
RENDER_KINDS = frozenset(
    {
        "assistant_text",
        "assistant_thinking",
        "tool_started",
        "tool_completed",
        "tool_failed",
        "prompt_suggestions",
        "system",
    }
)
PERMISSION_OPTIONS = [
    {
        "kind": "allow_once",
        "name": "Allow",
        "option_id": "allow",
        "permission_mode": None,
    },
    {
        "kind": "reject_once",
        "name": "Reject",
        "option_id": "reject",
        "permission_mode": None,
    },
]


class ProxyError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ActiveRun:
    message_id: str
    stream_token: str | None
    seq_base: int


def seq_from_stream_id(stream_id: str | None) -> int:
    """Ordin's Redis stream ids (`<ms>-<n>`) become the per-chat `seq` the
    frontend expects: time-ordered, so they keep increasing across turns and
    across restarts of this process."""
    if not stream_id:
        return 0
    ms, _, n = str(stream_id).partition("-")
    try:
        return int(ms) * 1000 + min(int(n or 0), 999)
    except ValueError:
        return 0


def stream_id_from_seq(seq: int) -> str | None:
    if seq <= 0:
        return None
    return f"{seq // 1000}-{seq % 1000}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_events(raw: str) -> list[dict[str, Any]] | None:
    if not raw or not raw.lstrip().startswith("["):
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(parsed, list):
        return None
    return [event for event in parsed if isinstance(event, dict)]


def _user_text(raw: str, events: list[dict[str, Any]] | None) -> str:
    if events is None:
        return raw
    return "".join(
        str(e.get("text", "")) for e in events if e.get("type") == "user_text"
    )


def ordin_permission_mode(mode: str | None) -> str:
    # Agentrove's per-agent modes collapse onto Ordin's three.
    return "plan" if mode == "plan" else "auto"


def ordin_thinking_mode(mode: str | None) -> str | None:
    if mode in ("low", "medium", "high", "ultra"):
        return mode
    if mode in ("xhigh", "max"):
        return "ultra"
    return None


def _elicitation_schema(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Claude Code's AskUserQuestion input as the flat object schema the
    frontend's question form renders. Keys are `question_<i>`, which is what
    Ordin wants back as `user_answers`."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    questions = tool_input.get("questions")
    if not isinstance(questions, list):
        questions = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        key = f"question_{index}"
        labels = [
            str(option.get("label"))
            for option in question.get("options") or []
            if isinstance(option, dict) and option.get("label")
        ]
        prop: dict[str, Any] = {
            "type": "string",
            "title": str(question.get("header") or question.get("question") or key),
            "description": str(question.get("question") or ""),
        }
        if labels:
            prop["enum"] = labels
        properties[key] = prop
        required.append(key)
    if not properties:
        properties["question_0"] = {"type": "string", "title": "Answer"}
        required.append("question_0")
    return {"type": "object", "properties": properties, "required": required}


async def _iter_sse(
    lines: AsyncIterator[str],
) -> AsyncIterator[tuple[str | None, str, str]]:
    event_id: str | None = None
    event = "message"
    data_lines: list[str] = []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines or event != "message":
                yield event_id, event, "\n".join(data_lines)
            event_id, event, data_lines = None, "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)


class OrdinProxyService:
    """Presents the user's Ordin chats through the API shape this app's
    frontend already speaks for remote instances, so cloud chats open,
    stream and continue in the normal chat view. State here is per process:
    the desktop sidecar serves one user."""

    def __init__(self, cloud: OrdinCloudService | None = None) -> None:
        self.cloud = cloud or ordin_cloud_service
        self._runs: dict[str, ActiveRun] = {}
        self._last_model: dict[str, str] = {}
        self._models: dict[UUID, tuple[float, list[dict[str, Any]]]] = {}

    # ---- chats -----------------------------------------------------------

    async def list_chats(
        self, user_id: UUID, page: int, per_page: int
    ) -> dict[str, Any]:
        body = await self._json(
            user_id, "GET", "/chat/chats", params={"page": page, "per_page": per_page}
        )
        return {
            "items": [self._map_chat(chat, user_id) for chat in body.get("items", [])],
            "page": body.get("page", page),
            "per_page": body.get("per_page", per_page),
            "total": body.get("total", 0),
            "pages": body.get("pages", 0),
        }

    async def get_chat(self, user_id: UUID, chat_id: str) -> dict[str, Any]:
        return self._map_chat(
            await self._json(user_id, "GET", f"/chat/chats/{chat_id}"), user_id
        )

    async def create_chat(self, user_id: UUID, title: str) -> dict[str, Any]:
        model_id = await self._default_model(user_id)
        body = await self._json(
            user_id,
            "POST",
            "/chat/chats",
            json_body={"title": title[:200] or "New chat", "model_id": model_id},
            expected=(200, 201),
        )
        return self._map_chat(body, user_id)

    async def update_chat(
        self, user_id: UUID, chat_id: str, patch: dict[str, Any]
    ) -> dict[str, Any]:
        allowed = {k: v for k, v in patch.items() if k in ("title", "pinned")}
        body = await self._json(
            user_id, "PATCH", f"/chat/chats/{chat_id}", json_body=allowed
        )
        return self._map_chat(body, user_id)

    async def delete_chat(self, user_id: UUID, chat_id: str) -> None:
        await self._json(
            user_id, "DELETE", f"/chat/chats/{chat_id}", expected=(200, 204)
        )
        self._runs.pop(chat_id, None)

    async def messages(
        self, user_id: UUID, chat_id: str, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        body = await self._json(
            user_id, "GET", f"/chat/chats/{chat_id}/messages", params=params
        )
        items = [self._map_message(message) for message in body.get("items", [])]
        for message in items:
            if message["role"] == "assistant" and message["model_id"]:
                self._last_model.setdefault(chat_id, message["model_id"])
                break
        return {
            "items": items,
            "next_cursor": body.get("next_cursor"),
            "has_more": bool(body.get("has_more")),
        }

    async def context_usage(self, user_id: UUID, chat_id: str) -> dict[str, Any]:
        body = await self._json(user_id, "GET", f"/chat/chats/{chat_id}/context-usage")
        return body if isinstance(body, dict) else {}

    # ---- turns -----------------------------------------------------------

    async def send(
        self,
        user_id: UUID,
        chat_id: str,
        prompt: str,
        permission_mode: str | None,
        thinking_mode: str | None,
    ) -> dict[str, Any]:
        status = await self._ordin_status(user_id, chat_id)
        if status.get("has_active_task"):
            raise ProxyError(409, "A response is already in progress for this chat")
        # The seq the client resumes from. Ordin clears its stream when the
        # worker picks the task up, but until then the previous run's tail is
        # still there, so replaying strictly after this id skips it.
        seq_base = seq_from_stream_id(status.get("last_event_id"))
        form: dict[str, Any] = {
            "prompt": prompt,
            "chat_id": chat_id,
            "model_id": await self._model_for(user_id, chat_id),
            "permission_mode": ordin_permission_mode(permission_mode),
        }
        thinking = ordin_thinking_mode(thinking_mode)
        if thinking:
            form["thinking_mode"] = thinking
        body = await self._json(user_id, "POST", "/chat/chat", data=form)
        message_id = str(body["message_id"])
        self._runs[chat_id] = ActiveRun(
            message_id=message_id,
            stream_token=body.get("stream_token"),
            seq_base=seq_base,
        )
        return {
            "chat_id": chat_id,
            "message_id": message_id,
            "last_seq": seq_base,
            "checkpoint_id": None,
            "worktree_cwd": None,
        }

    async def status(self, user_id: UUID, chat_id: str) -> dict[str, Any]:
        ordin = await self._ordin_status(user_id, chat_id)
        active = bool(ordin.get("has_active_task"))
        message_id = ordin.get("message_id")
        if active and message_id:
            run = self._runs.get(chat_id)
            self._runs[chat_id] = ActiveRun(
                message_id=str(message_id),
                stream_token=ordin.get("stream_token"),
                seq_base=run.seq_base
                if run and run.message_id == str(message_id)
                else 0,
            )
        else:
            self._runs.pop(chat_id, None)
        return {
            "has_active_task": active,
            "message_id": str(message_id) if message_id else None,
            "stream_id": str(message_id) if message_id else None,
            "last_seq": seq_from_stream_id(ordin.get("last_event_id")),
            "pending_elicitations": [],
        }

    async def active_streams(self, user_id: UUID) -> list[dict[str, Any]]:
        streams: list[dict[str, Any]] = []
        for chat_id in list(self._runs):
            status = await self.status(user_id, chat_id)
            if status["has_active_task"]:
                streams.append(
                    {
                        "chat_id": chat_id,
                        "message_id": status["message_id"],
                        "stream_id": status["stream_id"],
                        "last_seq": status["last_seq"],
                    }
                )
        return streams

    async def stop(self, user_id: UUID, chat_id: str) -> None:
        await self._json(
            user_id, "DELETE", f"/chat/chats/{chat_id}/stream", expected=(200, 204)
        )

    async def respond_permission(
        self, user_id: UUID, chat_id: str, request_id: str, option_id: str
    ) -> None:
        await self._respond(user_id, chat_id, request_id, approved=option_id == "allow")

    async def respond_elicitation(
        self,
        user_id: UUID,
        chat_id: str,
        request_id: str,
        action: str,
        content: dict[str, Any] | None,
    ) -> None:
        answers = json.dumps(content) if action == "accept" and content else None
        await self._respond(
            user_id,
            chat_id,
            request_id,
            approved=action == "accept",
            user_answers=answers,
        )

    async def _respond(
        self,
        user_id: UUID,
        chat_id: str,
        request_id: str,
        *,
        approved: bool,
        user_answers: str | None = None,
    ) -> None:
        form: dict[str, Any] = {"approved": "true" if approved else "false"}
        if user_answers:
            form["user_answers"] = user_answers
        response = await self.cloud.request(
            user_id,
            "POST",
            f"/chat/chats/{chat_id}/permissions/{request_id}/respond",
            data=form,
        )
        if response.status_code == 404:
            raise ProxyError(404, "This request has expired")
        if response.status_code != 200:
            raise ProxyError(
                502, ordin_error(response, "Ordin did not take the answer")
            )

    # ---- the multiplexed stream -------------------------------------------

    async def feed(
        self, user_id: UUID, cursors: dict[str, int]
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        tasks = [
            asyncio.create_task(self._follow(user_id, chat_id, cursor, queue))
            for chat_id, cursor in cursors.items()
        ]
        try:
            while True:
                yield await queue.get()
        finally:
            for task in tasks:
                task.cancel()

    async def _follow(
        self,
        user_id: UUID,
        chat_id: str,
        cursor: int,
        queue: asyncio.Queue[dict[str, Any]],
    ) -> None:
        try:
            status = await self._ordin_status(user_id, chat_id)
        except (AuthException, ProxyError) as e:
            logger.warning("Ordin status for chat %s failed: %s", chat_id, e)
            return
        if not status.get("has_active_task") or not status.get("stream_token"):
            return
        message_id = str(status["message_id"])
        run = self._runs.get(chat_id)
        seq_base = run.seq_base if run and run.message_id == message_id else 0
        self._runs[chat_id] = ActiveRun(
            message_id=message_id,
            stream_token=status["stream_token"],
            seq_base=seq_base,
        )
        last_seq = max(cursor, seq_base)

        def envelope(seq: int, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
            return {
                "event": "stream",
                "id": str(seq),
                "data": json.dumps(
                    {
                        "chatId": chat_id,
                        "messageId": message_id,
                        "streamId": message_id,
                        "seq": seq,
                        "kind": kind,
                        "payload": payload,
                    }
                ),
            }

        params: dict[str, Any] = {"stream_token": status["stream_token"]}
        if last_seq > 0:
            params["lastEventId"] = stream_id_from_seq(last_seq)
        if cursor == 0:
            last_seq += 1
            await queue.put(
                envelope(
                    last_seq,
                    "stream_started",
                    {"status": "started", "message_id": message_id},
                )
            )
        try:
            async with self.cloud.stream(f"/chat/chats/{chat_id}/stream", params) as (
                status_code,
                lines,
            ):
                if status_code != 200:
                    last_seq += 1
                    await queue.put(
                        envelope(
                            last_seq,
                            "error",
                            {"error": f"Ordin refused the stream (HTTP {status_code})"},
                        )
                    )
                    return
                async for event_id, event, data in _iter_sse(lines):
                    seq = seq_from_stream_id(event_id) if event_id else 0
                    if seq <= last_seq:
                        seq = last_seq + 1
                    for kind, payload in self._translate(event, data):
                        last_seq = seq
                        await queue.put(envelope(seq, kind, payload))
                        seq += 1
                    if event in ("complete", "error"):
                        self._runs.pop(chat_id, None)
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Following Ordin stream for chat %s failed: %s",
                chat_id,
                e,
                exc_info=True,
            )
            last_seq += 1
            await queue.put(
                envelope(last_seq, "error", {"error": "Lost the connection to Ordin"})
            )
            await queue.put(
                envelope(
                    last_seq + 1, "complete", {"status": "failed", "duration_ms": None}
                )
            )

    @staticmethod
    def _translate(event: str, data: str) -> list[tuple[str, dict[str, Any]]]:
        if event == "complete":
            try:
                parsed = json.loads(data) if data else {}
            except ValueError:
                parsed = {}
            if isinstance(parsed, dict) and parsed.get("status") == "cancelled":
                return [("cancelled", {"status": "interrupted", "duration_ms": None})]
            return [("complete", {"status": "completed", "duration_ms": None})]
        if event == "error":
            try:
                parsed = json.loads(data) if data else {}
            except ValueError:
                parsed = {}
            message = parsed.get("error") if isinstance(parsed, dict) else None
            return [
                ("error", {"error": str(message or "The response failed")}),
                ("complete", {"status": "failed", "duration_ms": None}),
            ]
        if event != "content":
            return []
        try:
            inner = json.loads(data).get("event") if data else None
        except (ValueError, AttributeError):
            return []
        if not isinstance(inner, dict):
            return []
        kind = inner.get("type")
        if kind == "permission_request":
            request_id = str(inner.get("request_id") or "")
            tool_name = str(inner.get("tool_name") or "")
            tool_input = inner.get("tool_input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            if tool_name == "AskUserQuestion":
                return [
                    (
                        "elicitation_request",
                        {
                            "request_id": request_id,
                            "data": {
                                "message": "The agent has a question",
                                "tool_call_id": None,
                                "requested_schema": _elicitation_schema(tool_input),
                            },
                        },
                    )
                ]
            return [
                (
                    "permission_request",
                    {
                        "request_id": request_id,
                        "tool_name": tool_name,
                        "tool_input": tool_input,
                        "data": {"options": PERMISSION_OPTIONS},
                    },
                )
            ]
        if kind == "system":
            payload = inner.get("data")
            return [("system", payload)] if isinstance(payload, dict) else []
        if kind in RENDER_KINDS or kind == "user_text":
            return [(kind, {k: v for k, v in inner.items() if k != "type"})]
        return []

    # ---- pseudo workspace and settings -----------------------------------

    def workspaces(self, user_id: UUID) -> dict[str, Any]:
        workspace = {
            "id": ORDIN_WORKSPACE_ID,
            "name": ORDIN_WORKSPACE_NAME,
            "user_id": str(user_id),
            "sandbox_id": "",
            "sandbox_provider": "docker",
            "workspace_path": "",
            "source_type": "local",
            "source_url": None,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "chat_count": 0,
            "last_chat_at": None,
        }
        return {"items": [workspace], "page": 1, "per_page": 25, "total": 1, "pages": 1}

    @staticmethod
    def workspace_resources() -> dict[str, Any]:
        # The composer iterates the list for the chat's agent kind, so every
        # kind needs an entry even though Ordin has no slash commands here.
        return {
            "skills": [],
            "builtin_slash_commands": {kind.value: [] for kind in AgentKind},
        }

    @staticmethod
    def settings(user_id: UUID) -> dict[str, Any]:
        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"ordin-settings:{user_id}")),
            "user_id": str(user_id),
            "github_personal_access_token": None,
            "custom_instructions": None,
            "custom_env_vars": [],
            "personas": None,
            "stream_actions": None,
            "notifications_enabled": True,
            "title_model_id": None,
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
        }

    # ---- helpers -----------------------------------------------------------

    async def _json(
        self,
        user_id: UUID,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        json_body: Any | None = None,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        try:
            response = await self.cloud.request(
                user_id, method, path, params=params, data=data, json_body=json_body
            )
        except AuthException as e:
            raise ProxyError(e.status_code, e.message) from e
        if response.status_code not in expected:
            status = (
                response.status_code
                if response.status_code in (400, 404, 409, 429)
                else 502
            )
            raise ProxyError(status, ordin_error(response, "Ordin request failed"))
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()

    async def _ordin_status(self, user_id: UUID, chat_id: str) -> dict[str, Any]:
        body = await self._json(user_id, "GET", f"/chat/chats/{chat_id}/status")
        return body if isinstance(body, dict) else {}

    async def _model_for(self, user_id: UUID, chat_id: str) -> str:
        model_id = self._last_model.get(chat_id)
        if model_id:
            return model_id
        return await self._default_model(user_id)

    async def _default_model(self, user_id: UUID) -> str:
        cached = self._models.get(user_id)
        if cached and time.monotonic() - cached[0] < MODELS_CACHE_SECONDS:
            models = cached[1]
        else:
            body = await self._json(user_id, "GET", "/models/")
            models = body if isinstance(body, list) else []
            self._models[user_id] = (time.monotonic(), models)
        for model in models:
            if (
                isinstance(model, dict)
                and model.get("enabled", True)
                and model.get("model_id")
            ):
                return str(model["model_id"])
        raise ProxyError(400, "No model is configured in Ordin for this account")

    def _map_chat(self, chat: dict[str, Any], user_id: UUID) -> dict[str, Any]:
        return {
            "id": str(chat["id"]),
            "user_id": str(user_id),
            "title": chat.get("title") or "Untitled chat",
            "workspace_id": ORDIN_WORKSPACE_ID,
            # Empty on purpose: it turns off the editor, diff and terminal panes,
            # which have no Ordin counterpart here.
            "sandbox_id": "",
            "created_at": chat["created_at"],
            "updated_at": chat["updated_at"],
            "context_token_usage": chat.get("context_token_usage") or 0,
            "pinned_at": chat.get("pinned_at"),
            "worktree_cwd": None,
            "parent_chat_id": None,
            "sub_thread_count": 0,
            "session_agent_kind": "claude",
            "unread": False,
            "last_model_id": self._last_model.get(str(chat["id"])),
            "last_thinking_mode": None,
            "last_persona_name": None,
        }

    @staticmethod
    def _map_message(message: dict[str, Any]) -> dict[str, Any]:
        role = str(message.get("role") or "assistant")
        raw = message.get("content") or ""
        events = _parse_events(raw)
        if role == "user":
            text = _user_text(raw, events)
            render = [{"type": "user_text", "text": text}]
            stream_status = "completed"
        else:
            if events is None:
                events = [{"type": "assistant_text", "text": raw}] if raw else []
            render = [event for event in events if event.get("type") in RENDER_KINDS]
            text = "".join(
                str(event.get("text", ""))
                for event in render
                if event.get("type") == "assistant_text"
            )
            stream_status = "completed" if raw else "in_progress"
        in_progress = stream_status == "in_progress"
        return {
            "id": str(message["id"]),
            "chat_id": str(message["chat_id"]),
            "role": role,
            "content_text": text,
            # The client seeds its render buffer from this and replays only
            # events after last_seq; an unfinished turn has nothing stored yet,
            # so it replays the whole run from seq 0.
            "content_render": {"events": render},
            "last_seq": 0,
            "active_stream_id": str(message["id"]) if in_progress else None,
            "stream_status": stream_status,
            "model_id": message.get("model_id"),
            "duration_ms": None,
            "attachments": [],
            "created_at": message["created_at"],
            "checkpoint_id": None,
        }


ordin_proxy_service = OrdinProxyService()
