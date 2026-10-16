"""FastAPI server — SSE streaming, HITL endpoints, Slack Bolt, and monitoring scheduler."""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from langgraph.types import Command
from langchain_core.messages import AIMessageChunk, ToolMessage
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from config import (
    CORS_ALLOW_ORIGINS,
    DATABASE_URL,
    DEFAULT_NAMESPACES,
    IN_CLUSTER,
    K8S_CONTEXT,
    MONITOR_ACK_HOURS,
    MONITORING_ENABLED,
    SLACK_APPROVER_IDS,
    SYSTEM_NAMESPACES,
    make_agent_config,
)
from demo_data import AUDIT, APPROVALS, DASHBOARD

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sre-agent")

# A "health check / audit" request is served by the bounded, single-Haiku-call
# structured path (scheduler.run_structured_health_check), NOT the full Deep
# Agents orchestrator — the orchestrator fans out to subagents and can blow past
# the langgraph recursion limit. Anything not matching this falls through to the
# agent as normal.
_HEALTH_CHECK_RE = re.compile(
    r"\b(health\s*[-]?\s*(check|audit|report|status)"
    r"|(cluster|cluster's)\s+health"
    r"|audit\s+(the\s+)?cluster"
    r"|(is|how'?s)\s+(the\s+)?cluster\s+(health|healthy|doing))\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# SkillHub — per-agent skill catalog
# ---------------------------------------------------------------------------
# Agent name → Chinese label, mirroring subagents/__init__.py (plus "main").
# Used only to render the SkillHub UI list; the agent names must match the
# ``name`` key in each subagent spec so DBSkillsMiddleware loads the right set.
SKILL_AGENTS: dict[str, str] = {
    "main": "主智能体",
    "pod-inspector": "Pod 巡检",
    "scaling-analyzer": "扩缩容分析",
    "performance-analyzer": "性能分析",
    "log-analyzer": "日志分析",
    "change-executor": "变更执行",
    "security-auditor": "安全审计",
    "reliability-auditor": "可靠性审计",
    "job-inspector": "任务巡检",
    "config-auditor": "配置审计",
}

_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

class SessionStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    DONE = "done"
    ERROR = "error"


@dataclass
class Session:
    id: str
    thread_id: str
    status: SessionStatus = SessionStatus.IDLE
    interrupt_data: Any = None
    pending_decisions: int = 1           # number of tool calls awaiting approval
    # Structured {"name": tool, "args": {...}} for each pending call. Kept
    # alongside interrupt_data (which is display-only stringified interrupts)
    # because the audit log has to record *what* was approved, not a repr.
    pending_actions: list = field(default_factory=list)
    last_response: str = ""
    title: str = ""                      # truncated first user message, for the sidebar
    source: str = "api"                  # 'api' | 'scheduler' | 'slack'
    slack_message_ts: Optional[str] = None
    slack_channel: Optional[str] = None  # channel to post responses back to
    slack_thread_ts: Optional[str] = None  # thread to reply in
    pending_hitl_actor: Optional[str] = None      # set on click, cleared on finalize
    pending_hitl_approved: Optional[bool] = None  # set on click, cleared on finalize
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)

    def to_row(self) -> dict:
        """Durable fields only — event_queue is per-process and not persisted."""
        return {
            "id": self.id,
            "thread_id": self.thread_id,
            "status": self.status.value,
            "source": self.source,
            "pending_decisions": self.pending_decisions,
            "pending_actions": self.pending_actions,
            "interrupt_data": self.interrupt_data,
            "last_response": self.last_response,
            "title": self.title,
            "slack_message_ts": self.slack_message_ts,
            "slack_channel": self.slack_channel,
            "slack_thread_ts": self.slack_thread_ts,
        }

    @classmethod
    def from_row(cls, row: dict) -> "Session":
        """Rebuild a session from Postgres with a fresh event queue."""
        return cls(
            id=row["id"],
            thread_id=row["thread_id"],
            status=SessionStatus(row["status"]),
            source=row.get("source") or "api",
            pending_decisions=row.get("pending_decisions") or 1,
            pending_actions=list(row.get("pending_actions") or []),
            interrupt_data=row.get("interrupt_data"),
            last_response=row.get("last_response") or "",
            title=row.get("title") or "",
            slack_message_ts=row.get("slack_message_ts"),
            slack_channel=row.get("slack_channel"),
            slack_thread_ts=row.get("slack_thread_ts"),
        )


_sessions: dict[str, Session] = {}
_executor = ThreadPoolExecutor(max_workers=4)
_notifier = None   # set in lifespan
_agent = None      # set in lifespan
_scheduler = None  # set in lifespan
_db = None         # set in lifespan (persistence.PostgresDatabase | NullDatabase)


def _save(session: Session) -> None:
    """Write a session through to Postgres.

    Best-effort by design: losing durability is bad, but it must never take down
    an in-flight cluster diagnosis. Failures are logged and surfaced by /health.
    """
    if _db is None:
        return
    try:
        _db.save_session(session.to_row())
    except Exception:
        log.exception("Failed to persist session %s", session.id)


def _track(session: Session) -> Session:
    _sessions[session.id] = session
    _save(session)
    return session


def _release_orphaned_sessions(db) -> None:
    """Mark sessions left mid-run by a previous process as errored.

    A run is an asyncio task, so a restart kills the task but never the stored
    status — and ``/api/chat`` refuses any message to a session whose status is
    ``running`` (409). One crash therefore bricks that conversation permanently:
    the operator cannot continue it and nothing in the UI explains why. Called
    once at startup, where no task can be running, so every ``running`` row is
    stale by definition. ``interrupted`` is deliberately untouched — waiting on
    a human decision is a state meant to survive a restart.
    """
    if db is None or not getattr(db, "available", False):
        return
    try:
        rows = db.list_sessions(200)
    except Exception:
        log.exception("Could not scan for sessions orphaned by the last shutdown")
        return

    for row in rows:
        if (row.get("status") or "") != SessionStatus.RUNNING.value:
            continue
        try:
            session = Session.from_row(row)
            session.status = SessionStatus.ERROR
            db.save_session(session.to_row())
            log.warning("Released session %s: was left running by a previous process",
                        session.id)
        except Exception:
            log.exception("Could not release orphaned session %s", row.get("id"))


def _recover_session(session_id: str) -> Optional[Session]:
    """Return a session from memory, falling back to Postgres.

    This is what makes a Slack Approve button survive a pod restart: the
    langgraph checkpoint still holds the interrupted graph under `thread_id`, so
    once the Session row is rehydrated the resume proceeds normally.
    """
    if session_id in _sessions:
        return _sessions[session_id]
    if _db is None:
        return None
    try:
        row = _db.load_session(session_id)
    except Exception:
        log.exception("Failed to load session %s", session_id)
        return None
    if not row:
        return None
    session = Session.from_row(row)
    _sessions[session_id] = session
    log.info(
        "Recovered session %s from Postgres (status=%s, pending=%d)",
        session.id, session.status, session.pending_decisions,
    )
    return session


def _get_session(session_id: str) -> Session:
    session = _recover_session(session_id)
    if session is None:
        raise HTTPException(404, f"Session '{session_id}' not found")
    return session


def _extract_action_requests(interrupts) -> list[dict]:
    """Pull structured {"name", "args"} pairs out of langgraph interrupts."""
    actions: list[dict] = []
    for interrupt in interrupts:
        val = interrupt.value if hasattr(interrupt, "value") else {}
        if not isinstance(val, dict):
            continue
        for req in val.get("action_requests", []) or []:
            if isinstance(req, dict):
                actions.append({
                    "name": req.get("name") or req.get("action") or "",
                    "args": req.get("args") or req.get("arguments") or {},
                })
    return actions


def _audit(session: Session, decision: str, actor: str = "", actor_id: str = "",
           result: str = "") -> None:
    """Append one immutable audit row per tool call covered by this decision."""
    if _db is None:
        return
    actions = session.pending_actions or []
    try:
        if not actions:
            _db.record_decision(
                session_id=session.id, thread_id=session.thread_id,
                decision=decision, actor=actor, actor_id=actor_id,
                source=session.source, result=result,
            )
            return
        for action in actions:
            _db.record_decision(
                session_id=session.id, thread_id=session.thread_id,
                decision=decision, actor=actor, actor_id=actor_id,
                source=session.source, tool_name=action.get("name", ""),
                tool_args=action.get("args") or {}, result=result,
            )
    except Exception:
        log.exception("Failed to write HITL audit row (session=%s)", session.id)


def _approver_allowed(user_id: str) -> bool:
    """Whether a Slack user may approve/reject a cluster mutation.

    An empty SLACK_APPROVER_IDS keeps the historical behaviour — anyone who can
    see the message can click — so turning on durability does not lock out an
    existing on-call rotation. Set it to restrict approvals to named users.
    """
    if not SLACK_APPROVER_IDS:
        return True
    return user_id in SLACK_APPROVER_IDS


# ---------------------------------------------------------------------------
# Agent runner helpers (run in thread pool to avoid blocking event loop)
# ---------------------------------------------------------------------------

def _extract_text(content) -> str:
    """Normalize agent message content to the text a human should read.

    Anthropic-compatible models can return ``content`` as a list of blocks
    rather than a plain string (DeepSeek's ``/anthropic`` endpoint does exactly
    this): a ``thinking`` block, text blocks, and — while the model is calling a
    tool — structural blocks such as ``tool_use`` and ``input_json_delta``.

    Only real text survives. Stringifying the structural blocks has put raw
    dicts in front of the user twice already: once as
    ``[object Object],[object Object]`` in the final message, and once as a
    stream of ``{'partial_json': ...}`` in the token stream, because the
    streaming path shares this function. Tool arguments are not the assistant's
    answer, so they are dropped rather than rendered.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                # A bare string in the list is text by construction.
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "thinking":
                    continue  # internal reasoning, not the user-facing answer
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                # Some providers hand back content blocks as objects rather
                # than dicts. Read a text field if one exists; never stringify
                # the object itself.
                if getattr(block, "type", None) == "thinking":
                    continue
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(p for p in parts if p)
    return str(content)


def _chunk_text(chunk) -> str:
    """Extract the text delta from a streamed message chunk (AI chunks only).

    ``stream_mode="messages"`` also yields ToolMessage chunks (subagent reports,
    tool results) whose content must not leak into the UI token stream. Only
    AI-generated text deltas are surfaced; reasoning/thinking blocks are dropped
    by ``_extract_text``.
    """
    if not isinstance(chunk, AIMessageChunk):
        return ""
    return _extract_text(chunk.content)


# ---------------------------------------------------------------------------
# Chain-of-thought steps
# ---------------------------------------------------------------------------
# The reasoning, tool calls and tool results the model produces are already in
# the stream — _extract_text/_chunk_text drop them because a chat bubble should
# carry only prose. The workbench's 思维链 panel wants the opposite, so it gets
# its own extractors; the token path is deliberately left alone, and these
# helpers must never be wired into it.
#
# The same block-walking code serves the live stream and the checkpoint replay,
# so the two cannot drift: on `done` the panel replaces its live view with the
# replayed one, and a difference there would show up as steps jumping or
# vanishing in front of the operator.

_COT_DETAIL_MAX = 500     # one-line summaries: fit a panel row without wrapping
_COT_RAW_MAX = 20000      # expandable original: enough to inspect, not to dump


def _block_get(block, key, default=None):
    """Read ``key`` from a content block that may be a dict or an object.

    Providers hand back both shapes (see ``_extract_text``); reading them
    through one accessor keeps the CoT walkers from growing two of everything.
    """
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _iter_blocks(content):
    """Yield ``(index, block)`` for each block of a message's content.

    ``index`` is the provider's own block index when it supplies one, else the
    position in the list. Streamed blocks carry one and the checkpoint keeps it,
    which is what lets a step keep its identity across both paths.
    """
    if content is None:
        return
    if isinstance(content, str):
        if content:
            yield 0, {"type": "text", "text": content}
        return
    if not isinstance(content, list):
        return
    for pos, block in enumerate(content):
        if isinstance(block, str):
            if block:
                yield pos, {"type": "text", "text": block}
            continue
        idx = _block_get(block, "index")
        yield (idx if isinstance(idx, int) else pos), block


def _tool_call_name_and_args(msg, block) -> tuple[str, Any]:
    """The name and arguments of one tool-call block.

    Three shapes are in play, in order of trustworthiness. The message's parsed
    ``tool_calls`` is the only fully-parsed form. Failing that, the block's
    ``partial_json`` — what the provider streams, and what the checkpoint
    actually stores (the ``input`` sitting next to it is empty, so trusting
    ``input`` first would show every call as argument-less). Last, ``input``.
    """
    name = _block_get(block, "name") or ""
    call_id = _block_get(block, "id") or ""

    for call in (getattr(msg, "tool_calls", None) or []):
        if not isinstance(call, dict):
            continue
        if call_id and call.get("id") not in (None, call_id):
            continue
        return call.get("name") or name, call.get("args")

    raw = _block_get(block, "partial_json")
    if isinstance(raw, str) and raw.strip():
        try:
            return name, json.loads(raw)
        except (TypeError, ValueError):
            # Half-written JSON: the call is still in flight. Show what arrived
            # rather than nothing — the panel marks it as running either way.
            return name, raw
    args = _block_get(block, "input")
    return name, {} if args is None else args


def _summarize_args(args, limit: int = 200) -> str:
    """One line describing a tool call's arguments."""
    if isinstance(args, dict):
        parts = []
        for key, value in args.items():
            if isinstance(value, str):
                value = " ".join(value.split())
            parts.append(f"{key}={value}")
        text = ", ".join(parts)
    elif isinstance(args, str):
        text = " ".join(args.split())
    else:
        text = ""
    return text[:limit]


def _cot_step(key: str, kind: str, node: str, title: str, detail: str = "",
              raw: Any = None, status: str = "done") -> dict:
    """One timeline entry. Flat and JSON-serializable: it goes on the wire as-is."""
    return {
        "key": key,
        "kind": kind,
        "node": node or "",
        "title": (title or "")[:_COT_DETAIL_MAX],
        "detail": (detail or "")[:_COT_DETAIL_MAX],
        "raw": raw if raw is None else str(raw)[:_COT_RAW_MAX],
        "status": status,
    }


def _steps_from_messages(messages) -> list[dict]:
    """The chain-of-thought timeline for a finished list of agent messages.

    Used for replay (the checkpoint's messages) and, unchanged, for the live
    view's authoritative refresh. Tool calls are paired with their results by
    ``tool_call_id``: the arguments are only final once the result lands, and
    the pair is what tells the panel whether a call is still running.
    """
    steps: list[dict] = []
    results = {}
    for msg in messages or []:
        if getattr(msg, "type", "") == "tool":
            results[getattr(msg, "tool_call_id", "")] = msg

    for pos, msg in enumerate(messages or []):
        mtype = getattr(msg, "type", "")
        # The checkpoint records which model/subagent produced a message; that
        # is the attribution the panel shows as 归属.
        node = getattr(msg, "name", "") or ""

        if mtype == "ai":
            for idx, block in _iter_blocks(getattr(msg, "content", None)):
                btype = _block_get(block, "type")

                if btype == "thinking":
                    text = _block_get(block, "thinking") or ""
                    if text.strip():
                        steps.append(_cot_step(f"{pos}:{idx}", "reasoning", node, text,
                                               raw=text))
                    continue

                if btype != "tool_use":
                    continue

                name, args = _tool_call_name_and_args(msg, block)
                call_id = _block_get(block, "id") or f"{pos}:{idx}"
                result = results.get(_block_get(block, "id") or "")
                summary = _summarize_args(args)
                # A `task` call is the orchestrator handing work to a subagent —
                # the fan-out an operator most wants to see named.
                if name == "task" and isinstance(args, dict):
                    sub = args.get("subagent_type") or args.get("agent") or "?"
                    title = f"派发子 agent：{sub}"
                else:
                    title = f"调用 {name or '(未命名工具)'}"
                steps.append(_cot_step(
                    f"call:{call_id}", "tool_call", node, title, summary,
                    raw=json.dumps(args, ensure_ascii=False, indent=2)
                    if not isinstance(args, str) else args,
                    status="done" if result is not None else "running",
                ))

        elif mtype == "tool":
            content = _extract_text(getattr(msg, "content", None))
            first = " ".join(content.split())[:200]
            steps.append(_cot_step(
                f"result:{getattr(msg, 'tool_call_id', '') or pos}", "tool_result", node,
                first or "(空结果)", f"{len(content)} 字符", raw=content,
            ))

    return steps


def _cot_steps_from_chunk(acc, metadata, key_prefix: str) -> list[dict]:
    """Timeline steps for the AI message currently being accumulated.

    ``acc`` is the running ``AIMessageChunk`` for one streamed message: the
    ``messages`` stream de-duplicates chunk ids and never re-emits the assembled
    message, so the caller has to accumulate. Steps are keyed on
    ``<message id>:<block index>``, so re-deriving them from the longer
    accumulation replaces the step in place instead of appending a duplicate.
    """
    node = (metadata or {}).get("langgraph_node", "") or ""
    steps: list[dict] = []
    for idx, block in _iter_blocks(getattr(acc, "content", None)):
        btype = _block_get(block, "type")

        if btype == "thinking":
            text = _block_get(block, "thinking") or ""
            if text.strip():
                steps.append(_cot_step(f"{key_prefix}:{idx}", "reasoning", node, text,
                                       raw=text))
            continue

        if btype != "tool_use":
            continue

        name, args = _tool_call_name_and_args(acc, block)
        call_id = _block_get(block, "id") or f"{key_prefix}:{idx}"
        complete = not isinstance(args, str)   # unparsable partial_json stays a str
        if name == "task" and isinstance(args, dict):
            sub = args.get("subagent_type") or args.get("agent") or "?"
            title = f"派发子 agent：{sub}"
        else:
            title = f"调用 {name or '(未命名工具)'}"
        steps.append(_cot_step(
            f"call:{call_id}", "tool_call", node, title, _summarize_args(args),
            raw=json.dumps(args, ensure_ascii=False, indent=2)
            if not isinstance(args, str) else args,
            status="done" if complete else "running",
        ))

    return steps


def _cot_result_step(msg) -> dict:
    """The tool_result step for a finished ToolMessage."""
    content = _extract_text(getattr(msg, "content", None))
    first = " ".join(content.split())[:200]
    return _cot_step(
        f"result:{getattr(msg, 'tool_call_id', '') or id(msg)}", "tool_result",
        getattr(msg, "name", "") or "", first or "(空结果)",
        f"{len(content)} 字符", raw=content,
    )


def _finalize_hitl_message(session: Session, result_text: str = ""):
    """If a HITL click was registered on this session, swap the 'processing' Slack
    message for the final verdict and clear the pending state."""
    if not (_notifier and session.slack_message_ts and session.pending_hitl_actor is not None):
        return
    _notifier.update_hitl_resolved(
        session.slack_message_ts,
        approved=bool(session.pending_hitl_approved),
        actor=session.pending_hitl_actor,
        result=result_text,
    )
    session.pending_hitl_actor = None
    session.pending_hitl_approved = None
    session.slack_message_ts = None
    _save(session)


def _handle_result(result: dict, session: Session, loop):
    """Process an agent result — put events on the session queue and send Slack msgs."""
    # Resolve any in-flight HITL message before posting a new one
    _finalize_hitl_message(session)

    interrupts = result.get("__interrupt__", [])

    if interrupts:
        session.status = SessionStatus.INTERRUPTED
        session.interrupt_data = [str(i) for i in interrupts]
        session.pending_actions = _extract_action_requests(interrupts)
        # Resume must send exactly one decision per pending tool call.
        session.pending_decisions = max(len(session.pending_actions), 1)

        # Persist before notifying: the Slack button must never point at a
        # session that was not written down.
        _save(session)

        # Notify via Slack
        if _notifier and _notifier.enabled:
            ts = _notifier.send_hitl_request(
                session.id,
                "\n".join(session.interrupt_data),
            )
            session.slack_message_ts = ts
            _save(session)

        # Surface the wait in the timeline too, and before the interrupt event:
        # the generator stops at `interrupt`, so a step queued after it is lost.
        # Live-only — it has no message in the checkpoint to be replayed from —
        # so the key only has to be unique, not to match anything on replay.
        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "cot", "data": _cot_step(
                f"hitl:{uuid.uuid4().hex[:8]}", "hitl", "sre-agent", "等待人工审批",
                "、".join(
                    a.get("name", "变更操作") for a in session.pending_actions
                ) or "待确认的变更操作",
                raw="\n".join(session.interrupt_data or []),
                status="running",
            )}),
            loop,
        )
        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "interrupt", "data": session.interrupt_data}),
            loop,
        )
    else:
        messages = result.get("messages", [])
        response = ""
        if messages:
            last = messages[-1]
            response = _extract_text(last.content) if hasattr(last, "content") else str(last)
        session.last_response = response
        session.status = SessionStatus.DONE
        session.pending_actions = []
        session.interrupt_data = None
        _save(session)

        # For scheduler-originated sessions, send Slack health report
        if session.source == "scheduler" and _notifier and _notifier.enabled:
            has_issues = any(
                kw in response.lower()
                for kw in ("critical", "crashloop", "oomkilled", "not ready", "evicted")
            )
            _notifier.send_health_report(response, has_issues=has_issues, source="scheduled")

        # `todos` goes before `done` on purpose: the SSE generator stops as soon
        # as it yields `done` (and the UI closes its EventSource), so anything
        # queued after it never reaches a client. Both ends used to be ordered
        # the other way, which is why no `todos` event has ever been delivered.
        todos = result.get("todos", [])
        if todos:
            asyncio.run_coroutine_threadsafe(
                session.event_queue.put({"type": "todos", "data": todos}),
                loop,
            )

        asyncio.run_coroutine_threadsafe(
            session.event_queue.put({"type": "done", "data": response}),
            loop,
        )


# Re-sending an accumulated step on every chunk would be quadratic: the model
# streams a token at a time and each event carries the whole block so far. Send
# one only once it has grown enough to be worth a redraw — the panel replaces
# its live view from /cot when the turn ends, so throttling loses nothing.
_COT_MIN_GROWTH = 200


async def _stream_agent(agent, payload, config: dict, session: Session, label: str):
    """Stream one agent turn into the session's queue.

    Shared by the initial run and the HITL resume. They differed only in their
    payload, and one copy is what keeps the 思维链 panel alive across an
    approval — a second, un-instrumented copy would go dark at the most
    interesting turn.

    Emits `token` (prose, for the chat bubble) and `cot` (timeline steps, for the
    panel). Nothing in this loop awaits or touches the database: every token
    passes through it, so one blocking call would stall the whole stream.
    """
    loop = asyncio.get_running_loop()
    # Per-invocation on purpose — state parked on the Session would leak a
    # half-finished turn into the one resumed after an approval.
    acc: dict[str, Any] = {}
    emitted: dict[str, tuple[int, str]] = {}

    def emit_steps(steps) -> None:
        for step in steps:
            size = len(step["title"]) + len(step.get("raw") or "")
            prev = emitted.get(step["key"])
            if prev is not None and prev[1] == step["status"] and size - prev[0] < _COT_MIN_GROWTH:
                continue
            emitted[step["key"]] = (size, step["status"])
            session.event_queue.put_nowait({"type": "cot", "data": step})

    try:
        final_state = None
        async for mode, chunk in agent.astream(
            payload,
            config=config,
            stream_mode=["messages", "values"],
        ):
            if mode == "messages":
                msg = chunk[0]
                metadata = chunk[1] if len(chunk) > 1 else {}
                if isinstance(msg, AIMessageChunk):
                    # The stream de-duplicates chunk ids and never re-emits the
                    # assembled message, so the blocks only make sense once
                    # accumulated.
                    mid = getattr(msg, "id", None) or "current"
                    acc[mid] = acc[mid] + msg if mid in acc else msg
                    emit_steps(_cot_steps_from_chunk(acc[mid], metadata, mid))
                elif isinstance(msg, ToolMessage):
                    emit_steps([_cot_result_step(msg)])
                text = _chunk_text(msg)
                if text:
                    session.event_queue.put_nowait({"type": "token", "data": text})
            elif mode == "values":
                final_state = chunk
        if final_state is not None:
            await asyncio.to_thread(_handle_result, final_state, session, loop)
    except Exception as e:
        log.exception("%s error (session=%s)", label, session.id)
        session.status = SessionStatus.ERROR
        await asyncio.to_thread(_save, session)
        session.event_queue.put_nowait({"type": "error", "data": str(e)})


async def _run_agent_async(agent, messages: list[dict], config: dict, session: Session):
    """Run the agent, streaming text and reasoning to the session queue."""
    await _stream_agent(agent, {"messages": messages}, config, session, "Agent")


async def _resume_agent_async(agent, command: Command, config: dict, session: Session):
    """Stream the resume of an interrupted agent (HITL approve/reject/edit)."""
    await _stream_agent(agent, command, config, session, "Resume")


def _do_approve(session: Session, loop):
    """Approve a HITL interrupt — usable from both API handlers and Slack Bolt threads."""
    session.status = SessionStatus.RUNNING
    _save(session)
    session.event_queue = asyncio.Queue()
    config = make_agent_config(session.thread_id)
    asyncio.run_coroutine_threadsafe(
        _resume_agent_async(
            _agent,
            Command(resume={"decisions": [{"type": "approve"}] * session.pending_decisions}),
            config,
            session,
        ),
        loop,
    )


def _do_reject(session: Session, reason: str, loop):
    """Reject a HITL interrupt — usable from both API handlers and Slack Bolt threads."""
    session.status = SessionStatus.RUNNING
    _save(session)
    session.event_queue = asyncio.Queue()
    config = make_agent_config(session.thread_id)
    asyncio.run_coroutine_threadsafe(
        _resume_agent_async(
            _agent,
            Command(resume={"decisions": [{"type": "reject", "message": reason}] * session.pending_decisions}),
            config,
            session,
        ),
        loop,
    )


# ---------------------------------------------------------------------------
# Slack chat helpers
# ---------------------------------------------------------------------------

_SLACK_MAX = 3000  # Slack text block character limit


def _post_long_response(client, channel: str, thread_ts: str, thinking_ts: str, response: str):
    """Post a response to Slack, handling long content gracefully.

    - Under 3000 chars: update the thinking message in place.
    - Over 3000 chars: update thinking message with a summary line, then upload
      the full content as a file snippet so nothing gets cut off.
    """
    if len(response) <= _SLACK_MAX:
        client.chat_update(channel=channel, ts=thinking_ts, text=response)
        return

    # First line of the response as a short summary
    summary = response.splitlines()[0][:200]
    client.chat_update(
        channel=channel,
        ts=thinking_ts,
        text=f"{summary}\n\n_Full output uploaded as a file below._",
    )
    client.files_upload_v2(
        channel=channel,
        thread_ts=thread_ts,
        content=response,
        filename="sre-agent-response.txt",
        title="Full Response",
    )


def _post_agent_result_to_slack(result: dict, session: Session, client, channel: str,
                                  thread_ts: str, thinking_ts: str):
    """Process agent result and update the Slack thinking message with the response."""
    interrupts = result.get("__interrupt__", [])
    if interrupts:
        session.status = SessionStatus.INTERRUPTED
        session.interrupt_data = [str(i) for i in interrupts]
        session.slack_channel = channel
        session.slack_thread_ts = thread_ts
        session.pending_actions = _extract_action_requests(interrupts)
        session.pending_decisions = max(len(session.pending_actions), 1)
        _save(session)

        if _notifier and _notifier.enabled:
            ts = _notifier.send_hitl_request(session.id, "\n".join(session.interrupt_data))
            session.slack_message_ts = ts
            _save(session)

        alerts_channel = os.getenv("SLACK_CHANNEL", "#sre-alerts")
        client.chat_update(
            channel=channel,
            ts=thinking_ts,
            text=f":warning: Action requires your approval — check {alerts_channel}",
        )
    else:
        messages = result.get("messages", [])
        response = ""
        if messages:
            last = messages[-1]
            response = _extract_text(last.content) if hasattr(last, "content") else str(last)
        session.last_response = response
        session.status = SessionStatus.DONE
        session.pending_actions = []
        session.interrupt_data = None
        _save(session)
        _post_long_response(client, channel, thread_ts, thinking_ts, response or "(no response)")


def _run_for_slack(text: str, session: Session, client, channel: str, thread_ts: str):
    """Run the agent for a Slack message and post the result back to the thread."""
    if not _agent:
        return
    thinking = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":hourglass_flowing_sand: Working on it...",
    )
    try:
        config = make_agent_config(session.thread_id)
        result = _agent.invoke({"messages": [{"role": "user", "content": text}]}, config=config)
        _post_agent_result_to_slack(result, session, client, channel, thread_ts, thinking["ts"])
    except Exception as e:
        session.status = SessionStatus.ERROR
        _save(session)
        log.exception("Slack agent error (session=%s)", session.id)
        client.chat_update(channel=channel, ts=thinking["ts"], text=f":red_circle: Error: {e}")


def _run_structured_health_check_for_slack(text: str, session: Session, client, channel: str, thread_ts: str):
    """Serve a health-check mention via the bounded single-Haiku path (no orchestrator).

    This performs a fixed number of steps (zero-token collection + one forced-tool
    Haiku call) so it can never hit the recursion limit that the full agent does.
    """
    thinking = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":mag: Running a cluster health check...",
    )
    try:
        from scheduler import annotate_with_history, run_structured_health_check

        report, data = run_structured_health_check()
        # Read-only diff: annotates each finding with age / occurrence count
        # without advancing the counters, which stay owned by the scheduler so
        # ad-hoc requests cannot inflate them. Skipped entirely without a
        # database, since with no history every finding would be labelled NEW
        # on every run, which is worse than showing no label at all.
        diff = (
            annotate_with_history(report, _db)
            if (_db is not None and _db.available) else None
        )
        log.info(
            "Slack health check complete (session=%s, severity=%s, findings=%d, %s)",
            session.id, report.overall_severity, len(report.findings),
            diff.summary_line() if diff else "no history",
        )
        if _notifier and _notifier.enabled:
            _notifier.send_structured_report(
                report, source="slack", channel=channel, thread_ts=thread_ts,
                diff=diff,
            )
            # The structured report is posted as its own threaded message; drop the
            # transient placeholder (best-effort — ignore missing chat:write scope).
            try:
                client.chat_delete(channel=channel, ts=thinking["ts"])
            except Exception:
                client.chat_update(channel=channel, ts=thinking["ts"],
                                   text=":white_check_mark: Health check complete.")
        else:
            client.chat_update(channel=channel, ts=thinking["ts"],
                               text=report.summary or "(no findings)")
        session.last_response = report.summary
        session.status = SessionStatus.DONE
        _save(session)
    except Exception as e:
        session.status = SessionStatus.ERROR
        _save(session)
        log.exception("Slack health check error (session=%s)", session.id)
        client.chat_update(channel=channel, ts=thinking["ts"], text=f":red_circle: Error: {e}")


def _resume_for_slack(command, session: Session, client):
    """Resume a HITL-interrupted session and post the result back to the original thread."""
    if not _agent:
        return
    channel = session.slack_channel
    thread_ts = session.slack_thread_ts
    if not channel or not thread_ts:
        return
    thinking = client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=":hourglass_flowing_sand: Continuing...",
    )
    try:
        config = make_agent_config(session.thread_id)
        result = _agent.invoke(command, config=config)
        _finalize_hitl_message(session)
        _post_agent_result_to_slack(result, session, client, channel, thread_ts, thinking["ts"])
    except Exception as e:
        session.status = SessionStatus.ERROR
        _save(session)
        log.exception("Slack resume error (session=%s)", session.id)
        _finalize_hitl_message(session, result_text=f"Error: {e}")
        client.chat_update(channel=channel, ts=thinking["ts"], text=f":red_circle: Error: {e}")


# ---------------------------------------------------------------------------
# Slack Bolt (Socket Mode — no public ingress required)
# ---------------------------------------------------------------------------

def _start_slack_bolt(main_loop: asyncio.AbstractEventLoop):
    """Start the Slack Bolt Socket Mode handler in a background thread."""
    bot_token = os.getenv("SLACK_BOT_TOKEN", "")
    app_token = os.getenv("SLACK_APP_TOKEN", "")
    if not bot_token or not app_token:
        log.info("SLACK_BOT_TOKEN or SLACK_APP_TOKEN not set — Slack Bolt not started")
        return

    try:
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        import re

        bolt = App(token=bot_token)

        @bolt.event("app_mention")
        def handle_mention(event, body, client):
            # Ignore Slack delivery retries — the first invocation is already in-flight
            if body.get("headers", {}).get("x-slack-retry-num"):
                return

            # Strip the @mention from the text
            text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text", "")).strip()
            if not text:
                client.chat_postMessage(
                    channel=event["channel"],
                    thread_ts=event["ts"],
                    text="Hi! Ask me anything about your cluster — e.g. _grab logs from pod X_ or _run a health audit_.",
                )
                return

            channel = event["channel"]
            # Use the thread root as the thread_id so context is preserved per thread
            thread_ts = event.get("thread_ts") or event["ts"]
            session_id = f"slack-{thread_ts}"

            session = _recover_session(session_id)
            if session is None:
                session = _track(Session(
                    id=session_id,
                    thread_id=session_id,
                    source="slack",
                    slack_channel=channel,
                    slack_thread_ts=thread_ts,
                ))
            else:
                if session.status in (SessionStatus.RUNNING, SessionStatus.INTERRUPTED):
                    log.info("Ignoring duplicate Slack event for session %s (status=%s)", session_id, session.status)
                    return
                session.status = SessionStatus.RUNNING
                session.slack_channel = channel
                session.slack_thread_ts = thread_ts
                _save(session)

            log.info("Slack mention from %s: %s", event.get("user"), text[:80])
            # Submit to executor so this handler returns immediately (prevents Slack retries).
            # Health-check/audit requests use the bounded single-Haiku path so they can
            # never hit the orchestrator's recursion limit; everything else goes to the agent.
            if _HEALTH_CHECK_RE.search(text):
                _executor.submit(_run_structured_health_check_for_slack, text, session, client, channel, thread_ts)
            else:
                _executor.submit(_run_for_slack, text, session, client, channel, thread_ts)

        def _resolve_click(body, client, verb: str):
            """Shared guard for approve/reject clicks.

            Returns ``(session, actor, actor_id)`` or None once it has already
            told the user why the click did nothing. Every rejection path
            replies — a silent return leaves someone believing they stopped a
            cluster change when they did not.
            """
            session_id = body["actions"][0]["value"]
            user = body.get("user", {}) or {}
            actor = user.get("name") or "unknown"
            actor_id = user.get("id") or ""
            channel_id = (body.get("channel") or {}).get("id")

            def deny(text: str):
                try:
                    client.chat_postEphemeral(channel=channel_id, user=actor_id, text=text)
                except Exception:
                    log.exception("Failed to post ephemeral reply to %s", actor_id)

            if not _approver_allowed(actor_id):
                log.warning(
                    "Unauthorized %s attempt by %s (%s) on session %s",
                    verb, actor, actor_id, session_id,
                )
                deny(
                    f":no_entry: You are not authorised to {verb} cluster changes. "
                    "Approvals are restricted to the users in SLACK_APPROVER_IDS."
                )
                # Record the attempt — a denied approval is exactly the kind of
                # event an audit trail exists for.
                denied = _recover_session(session_id)
                if denied is not None:
                    _audit(
                        denied, f"{verb}-denied", actor=actor, actor_id=actor_id,
                        result="actor not in SLACK_APPROVER_IDS",
                    )
                return None

            session = _recover_session(session_id)
            if session is None:
                deny(
                    f":warning: Session `{session_id}` not found. It predates the current "
                    "database, or its record was removed."
                )
                return None
            if session.status != SessionStatus.INTERRUPTED:
                deny(
                    f":warning: Session `{session_id}` is not waiting for approval "
                    f"(status={session.status.value})."
                )
                return None
            return session, actor, actor_id

        @bolt.action("sre_approve")
        def handle_approve(ack, body, client):
            ack()
            resolved = _resolve_click(body, client, "approve")
            if resolved is None:
                return
            session, actor, actor_id = resolved
            log.info("Slack approve from %s for session %s", actor, session.id)
            _audit(session, "approve", actor=actor, actor_id=actor_id)

            if _notifier and session.slack_message_ts:
                _notifier.mark_hitl_processing(session.slack_message_ts, actor, "Approval")
            session.pending_hitl_actor = actor
            session.pending_hitl_approved = True
            _save(session)

            if session.source == "slack":
                session.status = SessionStatus.RUNNING
                _save(session)
                _executor.submit(
                    _resume_for_slack,
                    Command(resume={"decisions": [{"type": "approve"}] * session.pending_decisions}),
                    session,
                    client,
                )
            else:
                _do_approve(session, main_loop)

        @bolt.action("sre_reject")
        def handle_reject(ack, body, client):
            ack()
            resolved = _resolve_click(body, client, "reject")
            if resolved is None:
                return
            session, actor, actor_id = resolved
            log.info("Slack reject from %s for session %s", actor, session.id)
            _audit(session, "reject", actor=actor, actor_id=actor_id)

            reason = ""
            if _notifier and session.slack_message_ts:
                _notifier.mark_hitl_processing(session.slack_message_ts, actor, "Rejection")
            session.pending_hitl_actor = actor
            session.pending_hitl_approved = False
            _save(session)

            if session.source == "slack":
                session.status = SessionStatus.RUNNING
                _save(session)
                _executor.submit(
                    _resume_for_slack,
                    Command(resume={"decisions": [{"type": "reject", "message": reason}] * session.pending_decisions}),
                    session,
                    client,
                )
            else:
                _do_reject(session, reason, main_loop)

        @bolt.action("sre_ack")
        def handle_ack(ack, body, client):
            """Mute every finding in a monitoring report for MONITOR_ACK_HOURS.

            Not gated by SLACK_APPROVER_IDS: acking changes no cluster state, it
            only silences notifications, and an on-call engineer should be able
            to quiet a known issue without needing mutation rights.
            """
            ack()
            report_id = body["actions"][0]["value"]
            user = body.get("user", {}) or {}
            actor = user.get("name") or "unknown"
            channel_id = (body.get("channel") or {}).get("id")

            acked = 0
            if _db is not None:
                try:
                    acked = _db.ack_report(report_id, MONITOR_ACK_HOURS)
                except Exception:
                    log.exception("Ack failed (report=%s)", report_id)

            log.info("Slack ack from %s for report %s (%d findings)", actor, report_id, acked)
            if acked:
                client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=(body.get("message") or {}).get("ts"),
                    text=(
                        f":mute: {actor} acked {acked} finding(s) for {MONITOR_ACK_HOURS}h — "
                        "they will stay tracked but will not re-alert until then."
                    ),
                )
            else:
                try:
                    client.chat_postEphemeral(
                        channel=channel_id,
                        user=user.get("id", ""),
                        text=(
                            ":warning: Nothing to ack — these findings were already "
                            "resolved, or this report predates the current database."
                        ),
                    )
                except Exception:
                    log.exception("Failed to post ephemeral ack reply")

        handler = SocketModeHandler(bolt, app_token)
        log.info("Starting Slack Bolt Socket Mode handler")
        handler.start()
    except Exception:
        log.exception("Slack Bolt failed to start")


# ---------------------------------------------------------------------------
# FastAPI app + lifespan
# ---------------------------------------------------------------------------

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _notifier, _agent, _scheduler, _db

    # 1. Durable state (Postgres, or in-memory fallback). Built first so the
    #    agent, session table, audit log, and scheduler all share one pool.
    from persistence import init_persistence
    checkpointer, store, _db = init_persistence(DATABASE_URL)

    # 1b. Release sessions orphaned by the last shutdown. A run lives in an
    #     asyncio task, so a restart mid-run leaves the row saying "running" with
    #     nothing behind it — and /api/chat refuses to continue a running
    #     session, which locks the operator out of that conversation for good.
    #     At startup no task is running, so every `running` row is stale by
    #     definition. `interrupted` is left alone: waiting for a human decision
    #     is a state that is meant to survive a restart.
    _release_orphaned_sessions(_db)

    # 2. Slack notifier
    from slack_notifier import make_notifier
    _notifier = make_notifier()

    # 3. Slack tool
    from tools.slack import make_slack_notification_tool
    slack_tool = make_slack_notification_tool(_notifier)

    # 4. Agent (with Slack tool injected)
    from agent import create_sre_agent
    _agent = create_sre_agent(
        extra_tools=[slack_tool], checkpointer=checkpointer, store=store, db=_db
    )

    # 5. Slack Bolt Socket Mode in background thread
    main_loop = asyncio.get_event_loop()
    threading.Thread(target=_start_slack_bolt, args=(main_loop,), daemon=True).start()

    # 6. Monitoring scheduler. MONITORING_ENABLED is now actually honoured — it
    #    was previously documented and set in the manifest but read by no code,
    #    so scheduled checks ran regardless of the flag.
    from scheduler import MonitoringScheduler
    interval = int(os.getenv("MONITOR_INTERVAL_MINUTES", "30"))
    _scheduler = MonitoringScheduler(_agent, _notifier, interval_minutes=interval, db=_db)
    if MONITORING_ENABLED:
        await _scheduler.start()
    else:
        log.info("MONITORING_ENABLED is false — scheduled health checks not started")

    if _notifier.enabled:
        _notifier.send_alert("ok", "SRE Bot Started", "The autonomous SRE bot is online and monitoring the cluster.")

    if not _db.available:
        log.warning(
            "Running with NO durable state — pending HITL approvals will not survive a "
            "restart and no audit trail is being written. Set DATABASE_URL to fix."
        )

    log.info(
        "SRE Bot ready (scheduler=%dm, slack=%s, state=%s, approvers=%s)",
        interval, _notifier.enabled, _db.kind,
        f"{len(SLACK_APPROVER_IDS)} allowlisted" if SLACK_APPROVER_IDS else "unrestricted",
    )
    yield

    # Shutdown
    if _scheduler:
        await _scheduler.stop()
    if _db is not None:
        _db.close()
    log.info("SRE Bot shutdown")


app = FastAPI(title="SRE Bot", version="1.0.0", lifespan=lifespan)
# Empty by default. The bundled UI is same-origin so it needs no CORS grant, and
# a wildcard here let any page read /api/audit through an open port-forward.
# NOTE: this blocks the browser path only. Anything that can reach the port
# directly still has unauthenticated access to every /api route.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class ApproveRequest(BaseModel):
    session_id: str


class RejectRequest(BaseModel):
    session_id: str
    reason: str = ""


class EditRequest(BaseModel):
    session_id: str
    args: dict[str, Any]


class SkillCreateRequest(BaseModel):
    skill_name: str
    description: str = ""
    content: str = ""


class SkillUpdateRequest(BaseModel):
    description: Optional[str] = None
    content: Optional[str] = None


class SkillToggleRequest(BaseModel):
    enabled: bool


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    # Deliberately still "ok" when Postgres is down: this is the liveness probe,
    # and crash-looping the bot because its database is unreachable would remove
    # the operator's ability to ask it what is wrong. `durable_state` is the
    # field to alert on — it makes the degradation visible instead of silent.
    return {
        "status": "ok",
        "in_cluster": os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token"),
        "slack_enabled": _notifier.enabled if _notifier else False,
        "scheduler_running": _scheduler._running if _scheduler else False,
        "state_backend": _db.kind if _db else "uninitialized",
        "durable_state": _db.available if _db else False,
        "approvals_restricted": bool(SLACK_APPROVER_IDS),
    }


@app.get("/api/audit")
def audit_trail(limit: int = 50):
    """Recent HITL decisions — who approved or rejected which cluster change."""
    if _db is None or not _db.available:
        raise HTTPException(503, "No audit database configured (set DATABASE_URL)")
    rows = _db.recent_decisions(limit)
    return {
        "count": len(rows),
        "decisions": [
            {
                "ts": r["ts"].isoformat() if r.get("ts") else None,
                "session_id": r.get("session_id"),
                "actor": r.get("actor"),
                "actor_id": r.get("actor_id"),
                "decision": r.get("decision"),
                "source": r.get("source"),
                "tool_name": r.get("tool_name"),
                "tool_args": r.get("tool_args"),
                "result": r.get("result"),
            }
            for r in rows
        ],
    }


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Start or continue a conversation. Returns session_id for SSE streaming."""
    session = _recover_session(req.session_id) if req.session_id else None
    if session is None:
        session_id = str(uuid.uuid4())
        # Title the sidebar entry from the user's first message (whitespace
        # collapsed). Only set here — continuing a session must not overwrite it.
        title = " ".join(req.message.split())[:50]
        session = _track(Session(id=session_id, thread_id=session_id, source="api", title=title))

    if session.status == SessionStatus.RUNNING:
        raise HTTPException(409, "Session is already running")

    session.status = SessionStatus.RUNNING
    _save(session)
    session.event_queue = asyncio.Queue()
    asyncio.create_task(
        _run_agent_async(
            _agent,
            [{"role": "user", "content": req.message}],
            make_agent_config(session.thread_id),
            session,
        )
    )
    return {"session_id": session.id, "status": "running"}


@app.get("/api/sessions/{session_id}/events")
async def stream_events(session_id: str):
    """SSE stream — yields token / cot / done / interrupt / todos / error /
    heartbeat events. Any queued event is forwarded as-is, so new types need no
    change here; the terminal ones (done/error/interrupt) end the stream."""
    session = _get_session(session_id)

    async def generator():
        while True:
            try:
                event = await asyncio.wait_for(session.event_queue.get(), timeout=60.0)
                yield json.dumps(event)
                if event["type"] in ("done", "error"):
                    break
                if event["type"] == "interrupt":
                    break
            except asyncio.TimeoutError:
                yield json.dumps({"type": "heartbeat"})

    return EventSourceResponse(generator())


@app.get("/api/sessions/{session_id}")
def get_session_status(session_id: str):
    s = _get_session(session_id)
    return {
        "session_id": s.id,
        "status": s.status,
        "source": s.source,
        "last_response": s.last_response,
        "interrupt_data": s.interrupt_data,
    }


def _session_title(s: Session) -> str:
    """Sidebar label: the recorded title, else the tail of the last response."""
    return (s.title or "").strip() or (s.last_response or "").strip()[:40] or "(未命名会话)"


@app.get("/api/sessions")
def list_sessions():
    """Session history for the workbench sidebar, newest first."""
    if _db is not None and _db.available:
        try:
            rows = _db.list_sessions(50)
        except Exception:
            log.exception("Failed to list sessions")
            rows = []
        items = []
        for r in rows:
            title = (r.get("title") or "").strip() or (r.get("last_response") or "").strip()[:40]
            updated = r.get("updated_at")
            items.append({
                "id": r["id"],
                "thread_id": r.get("thread_id"),
                "title": title or "(未命名会话)",
                "status": r.get("status"),
                "source": r.get("source"),
                "updated_at": updated.isoformat() if updated is not None else None,
            })
        return {"source": "live", "items": items}

    # Degraded (no Postgres): serve the in-process session cache.
    items = [
        {
            "id": s.id,
            "thread_id": s.thread_id,
            "title": _session_title(s),
            "status": s.status.value,
            "source": s.source,
            "updated_at": None,
        }
        for s in _sessions.values()
    ]
    return {"source": "memory", "items": items}


@app.get("/api/sessions/{session_id}/messages")
def session_messages(session_id: str):
    """Full conversation history for one session, read from the langgraph checkpoint."""
    session = _get_session(session_id)
    messages: list[dict] = []
    if _agent is not None:
        try:
            snapshot = _agent.get_state(make_agent_config(session.thread_id))
            raw = (snapshot.values or {}).get("messages", []) if snapshot else []
        except Exception:
            log.exception("Failed to read messages for session %s", session_id)
            raw = []
        for m in raw:
            mtype = getattr(m, "type", "")
            content = _extract_text(getattr(m, "content", None))
            if not content:
                continue
            if mtype == "human":
                messages.append({"role": "user", "content": content})
            elif mtype == "ai":
                messages.append({"role": "assistant", "content": content})
    return {"messages": messages}


@app.get("/api/sessions/{session_id}/cot")
def session_cot(session_id: str):
    """Chain-of-thought timeline for one session, read from the same checkpoint.

    Replay comes from the checkpoint rather than a purpose-built table because
    the reasoning and tool-call blocks are already durable there — which means
    sessions that predate this feature still replay. A new table would start
    empty and show a blank panel for exactly the case the panel exists for.
    """
    session = _get_session(session_id)
    steps: list[dict] = []
    if _agent is not None:
        try:
            snapshot = _agent.get_state(make_agent_config(session.thread_id))
            raw = (snapshot.values or {}).get("messages", []) if snapshot else []
        except Exception:
            log.exception("Failed to read chain of thought for session %s", session_id)
            raw = []
        steps = _steps_from_messages(raw)[-200:]
    return {"steps": steps, "status": session.status.value}


@app.post("/api/approve")
async def approve(req: ApproveRequest):
    """Approve a pending HITL action."""
    session = _get_session(req.session_id)
    if session.status != SessionStatus.INTERRUPTED:
        raise HTTPException(409, f"Session not interrupted (status={session.status})")
    if _notifier and session.slack_message_ts:
        _notifier.mark_hitl_processing(session.slack_message_ts, "api-user", "Approval")
    # NOTE: this endpoint is unauthenticated, so the audit trail can only record
    # "api-user". Slack clicks carry a real identity; HTTP approvals do not.
    _audit(session, "approve", actor="api-user")
    session.pending_hitl_actor = "api-user"
    session.pending_hitl_approved = True
    _do_approve(session, asyncio.get_event_loop())
    return {"session_id": session.id, "status": "running"}


@app.post("/api/reject")
async def reject(req: RejectRequest):
    """Reject a pending HITL action."""
    session = _get_session(req.session_id)
    if session.status != SessionStatus.INTERRUPTED:
        raise HTTPException(409, f"Session not interrupted (status={session.status})")
    if _notifier and session.slack_message_ts:
        _notifier.mark_hitl_processing(session.slack_message_ts, "api-user", "Rejection")
    _audit(session, "reject", actor="api-user", result=req.reason)
    session.pending_hitl_actor = "api-user"
    session.pending_hitl_approved = False
    _do_reject(session, req.reason, asyncio.get_event_loop())
    return {"session_id": session.id, "status": "running"}


@app.post("/api/edit")
async def edit(req: EditRequest):
    """Edit the proposed action arguments then resume."""
    session = _get_session(req.session_id)
    if session.status != SessionStatus.INTERRUPTED:
        raise HTTPException(409, f"Session not interrupted (status={session.status})")
    # An edit is an approval with modified arguments — record what was actually
    # authorised, not just that something was.
    _audit(session, "edit", actor="api-user", result=json.dumps(req.args)[:2000])
    session.status = SessionStatus.RUNNING
    _save(session)
    session.event_queue = asyncio.Queue()
    asyncio.create_task(
        _resume_agent_async(
            _agent,
            Command(resume={"decisions": [{"type": "edit", "args": req.args}] * session.pending_decisions}),
            make_agent_config(session.thread_id),
            session,
        )
    )
    return {"session_id": session.id, "status": "running"}


@app.post("/api/trigger-check")
async def trigger_check():
    """Manually trigger an immediate health check outside the schedule."""
    if not _scheduler:
        raise HTTPException(503, "Scheduler not initialized")
    session_id = await _scheduler.trigger_now()
    return {"session_id": session_id, "status": "running"}


# ---------------------------------------------------------------------------
# Read-only ops console panels (sre-agent-ui, served under /ui/)
# ---------------------------------------------------------------------------
# These back the static dashboard. They read live data where a real source exists
# (the Qdrant runbook index) and fall back to structured demo data where this
# environment has none (no kubectl context, DATABASE_URL unset → no findings /
# audit / approvals). Every payload carries "source": "live" | "demo" so the UI
# can label what is real versus illustrated.

_RUNBOOK_CATEGORY = {
    "Pod": "Pod 排障",
    "Node": "节点",
    "Service": "网络",
    "Deployment": "工作负载",
    "Ingress": "网络",
}


def _runbook_blurb(payload: dict) -> str:
    """First human-readable line of a chunk, with markdown markers stripped."""
    content = str(payload.get("content", "")).strip()
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^[-*]\s+", "", line)
        line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
        line = line.strip()
        if line:
            return line[:80]
    return ""


def _ui_runbooks() -> dict:
    from rag.vector_store import QdrantRunbookStore, VectorStoreUnavailableError

    try:
        chunks = QdrantRunbookStore().all_chunks()
    except (VectorStoreUnavailableError, Exception) as exc:  # noqa: BLE001
        log.exception("Runbook library unavailable")
        return {"source": "demo", "count": 0, "runbooks": [], "error": str(exc)}

    grouped: dict[str, dict] = {}
    for payload in chunks:
        title = str(payload.get("title") or payload.get("title_path", ["(未命名)"])[0])
        entry = grouped.setdefault(
            title,
            {
                "name": title,
                "description": _runbook_blurb(payload),
                "resource_kind": payload.get("resource_kind") or "其他",
                "source_url": payload.get("source_url") or payload.get("source") or "",
                "chunks": 0,
            },
        )
        entry["chunks"] += 1

    runbooks = []
    for entry in grouped.values():
        entry["category"] = _RUNBOOK_CATEGORY.get(entry["resource_kind"], "诊断")
        runbooks.append(entry)
    runbooks.sort(key=lambda r: r["name"])
    return {"source": "live", "count": len(runbooks), "runbooks": runbooks}


@app.get("/api/ui/runbooks")
def ui_runbooks():
    return _ui_runbooks()


@app.get("/api/ui/runbooks/search")
def ui_runbooks_search(q: str = ""):
    query = (q or "").strip()
    if not query:
        return {"source": "live", "count": 0, "results": []}
    from rag.embeddings import embed_query
    from rag.vector_store import QdrantRunbookStore, VectorStoreUnavailableError

    try:
        results = QdrantRunbookStore().search(embed_query(query), limit=6)
    except Exception as exc:  # noqa: BLE001
        log.exception("Runbook search failed")
        return {"source": "demo", "count": 0, "results": [], "error": str(exc)}

    return {
        "source": "live",
        "count": len(results),
        "results": [
            {
                "heading": " > ".join(map(str, r.payload.get("title_path") or []))
                or r.payload.get("title", "Runbook"),
                "content": str(r.payload.get("content", ""))[:600],
                "score": round(r.score, 3),
                "source_url": r.payload.get("source_url") or r.payload.get("source") or "",
            }
            for r in results
        ],
    }


@app.get("/api/ui/dashboard")
def ui_dashboard():
    payload = dict(DASHBOARD)
    # Live: count in-memory interrupted sessions (pending HITL approvals).
    pending = sum(1 for s in _sessions.values() if s.status == SessionStatus.INTERRUPTED)
    if pending:
        payload["pending_approvals"] = str(pending)
        payload["source"] = "live"
    return payload


_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _friendly_cluster_error(errors: list[str]) -> str:
    """Turn collection errors into a short, human-readable 'no cluster' reason."""
    joined = " ".join(errors)
    low = joined.lower()
    if "config" in low or "kube" in low or "configexception" in low:
        return "未检测到可用的集群配置（kubeconfig / kubectl 上下文缺失）"
    if "connection" in low or "timeout" in low or "refused" in low or "unreachable" in low:
        return "无法连接 Kubernetes API Server（连接被拒绝或超时）"
    if not errors:
        return "未检测到集群数据"
    return "采集失败：" + (errors[0][:120] if errors else "")


def _ui_health_payload() -> dict:
    """Real cluster snapshot for the read-only 健康检查 panel.

    Uses the scheduler's zero-LLM ``_collect_cluster_data`` (direct kubernetes
    client calls) and joins it with the persisted finding state, so the panel
    reflects what the autonomous monitor actually sees. When there is no
    kubeconfig / cluster, every collection block errors and the lists stay empty
    — the UI shows that as a clear "未连接集群" state rather than demo numbers.
    """
    from datetime import datetime, timezone
    from scheduler import (
        PVC_USAGE_ALERT_PERCENT,
        _collect_cluster_data,
        _cpu_millicores,
        _memory_ki,
    )

    data = _collect_cluster_data()
    errors = [str(e) for e in (data.get("errors") or [])]

    nodes = data.get("nodes") or []
    pods = data.get("pods") or []
    unhealthy_pods = data.get("unhealthy_pods") or []
    events = data.get("events") or []
    hpas = data.get("hpas") or []
    deployments = data.get("deployments") or []
    pvc_usage = data.get("pvc_usage") or {}

    # Reachability: if *any* list call returned rows we have a cluster; if every
    # block errored (no kubeconfig), every list is empty — that's "no cluster",
    # not "an empty cluster".
    connected = bool(nodes or pods or deployments or hpas)

    nodes_total = len(nodes)
    nodes_ready = sum(1 for n in nodes if n.get("status") == "Ready")

    deployments_degraded = [d for d in deployments if d["ready"] < d["desired"]]

    hpa_at_max = []
    for h in hpas:
        cur, mx = h.get("current"), h.get("max")
        if isinstance(cur, int) and isinstance(mx, int) and mx > 0 and cur >= mx:
            hpa_at_max.append(h)

    pvc_list = sorted(
        pvc_usage.items(), key=lambda kv: kv[1]["percent"], reverse=True
    )
    pvc_high = [
        {"key": key, **value}
        for key, value in pvc_list
        if value["percent"] >= PVC_USAGE_ALERT_PERCENT
    ]

    # Node utilisation, joined to allocatable so the UI can draw a real % bar.
    alloc = {n["name"]: n for n in nodes}
    node_metrics = []
    for m in data.get("node_metrics") or []:
        node = alloc.get(m["name"], {})
        cpu_used = _cpu_millicores(m.get("cpu"))
        cpu_cap = _cpu_millicores(node.get("cpu_allocatable"))
        mem_used = _memory_ki(m.get("memory"))
        mem_cap = _memory_ki(node.get("memory_allocatable"))
        node_metrics.append({
            "name": m["name"],
            "cpu_used": m.get("cpu"),
            "memory_used": m.get("memory"),
            "cpu_percent": round(cpu_used / cpu_cap * 100, 1) if cpu_used is not None and cpu_cap else None,
            "memory_percent": round(mem_used / mem_cap * 100, 1) if mem_used is not None and mem_cap else None,
        })

    # Open findings from finding_state (persisted by the scheduler across runs).
    findings = []
    if _db is not None and _db.available:
        now = datetime.now(timezone.utc)
        for f in _db.load_tracked_findings().values():
            if f.resolved_at is not None:
                continue
            findings.append({
                "fingerprint": f.fingerprint,
                "severity": f.severity,
                "title": f.title,
                "namespace": f.namespace,
                "first_seen": f.first_seen.isoformat() if f.first_seen else None,
                "last_seen": f.last_seen.isoformat() if f.last_seen else None,
                "times_seen": f.times_seen,
                "acked": bool(f.ack_until and f.ack_until > now),
            })
    findings.sort(key=lambda f: _SEVERITY_ORDER.get(f.get("severity"), 9))

    critical_findings = sum(1 for f in findings if f.get("severity") == "critical")

    # Health score derived only from live collection data (findings are history).
    score = None
    if connected:
        score = 100
        score -= min(40, 8 * len(unhealthy_pods))
        score -= min(20, 10 * (nodes_total - nodes_ready))
        score -= min(20, 4 * len(deployments_degraded))
        score -= min(15, 5 * len(hpa_at_max))
        score -= min(15, 2 * len(pvc_high))
        score -= min(10, 1 * len(events))
        score = max(0, score)

    return {
        "source": "live" if connected else "empty",
        "connected": connected,
        "cluster_error": None if connected else _friendly_cluster_error(errors),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "health_score": score,
        "summary": {
            "nodes_total": nodes_total,
            "nodes_ready": nodes_ready,
            "pods_total": len(pods),
            "unhealthy_pods": len(unhealthy_pods),
            "warning_events": len(events),
            "deployments_total": len(deployments),
            "deployments_degraded": len(deployments_degraded),
            "hpa_total": len(hpas),
            "hpa_at_max": len(hpa_at_max),
            "pvc_total": len(pvc_usage),
            "pvc_high": len(pvc_high),
            "open_findings": len(findings),
            "critical_findings": critical_findings,
        },
        "nodes": nodes,
        "unhealthy_pods": unhealthy_pods,
        "events": events[:10],
        "deployments_degraded": deployments_degraded,
        "hpa_at_max": hpa_at_max,
        "node_metrics": node_metrics,
        "pvc_high": pvc_high,
        "findings": findings,
        "errors": errors,
        "thresholds": {"pvc_alert_percent": PVC_USAGE_ALERT_PERCENT},
    }


@app.get("/api/ui/health")
def ui_health():
    try:
        return _ui_health_payload()
    except Exception as exc:  # noqa: BLE001
        log.exception("ui health payload failed")
        return {
            "source": "error",
            "connected": False,
            "cluster_error": f"健康检查接口异常：{exc}",
            "checked_at": None,
            "health_score": None,
            "summary": {},
            "nodes": [], "unhealthy_pods": [], "events": [],
            "deployments_degraded": [], "hpa_at_max": [], "node_metrics": [],
            "pvc_high": [], "findings": [], "errors": [str(exc)],
            "thresholds": {},
        }


# Allowed trend windows for the data-centre page, in seconds. Bounded rather
# than free-form: each one costs a Prometheus range query, and the step is
# chosen so the series stays under ~180 points regardless of window — past that
# the hand-rolled SVG line has more vertices than the panel has pixels.
_UI_DC_RANGES = {
    "15m": (900, 15),
    "1h": (3600, 60),
    "6h": (21600, 180),
    "24h": (86400, 600),
}


@app.get("/api/ui/datacenter")
def ui_datacenter(range: str = "1h"):
    """Cluster metrics sourced from Prometheus, for the data-centre page.

    `source` mirrors the other console panels: "live" when Prometheus answered,
    "unavailable" when it did not. The distinction matters here more than
    elsewhere — a metrics dashboard that silently renders zeros for an
    unreachable backend reads as a healthy cluster.
    """
    from datetime import datetime, timezone

    from tools.prometheus import (
        PrometheusUnavailable,
        active_targets,
        base_url,
        resource_usage,
        snapshot,
    )

    checked_at = datetime.now(timezone.utc).isoformat()
    window, step = _UI_DC_RANGES.get(range, _UI_DC_RANGES["1h"])

    try:
        data = snapshot(range_seconds=window, step_seconds=step, top_n=6)
        # Same try: this is several more queries against the same backend, so a
        # backend that died between the two calls degrades the whole panel
        # rather than half-rendering it.
        quotas = resource_usage(top_n=8)
    except PrometheusUnavailable as exc:
        log.warning("datacenter panel: Prometheus unavailable: %s", exc)
        return {
            "source": "unavailable",
            "connected": False,
            "error": str(exc),
            "prometheus_url": base_url(),
            "checked_at": checked_at,
            "range": range,
        }

    return {
        "source": "live",
        "connected": True,
        "prometheus_url": base_url(),
        "checked_at": checked_at,
        "range": range,
        "targets": active_targets(),
        "quotas": quotas,
        **data,
    }


def _ui_dc_resource(collect, label: str) -> dict:
    """Shared envelope for the per-resource datacenter panels.

    Same source=live/unavailable contract as the overview endpoint: an
    unreachable Prometheus must not render as an empty-but-healthy resource
    list, which on a Pod page would read as "no pods are failing".
    """
    from datetime import datetime, timezone

    from tools.prometheus import PrometheusUnavailable, base_url

    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        data = collect()
    except PrometheusUnavailable as exc:
        log.warning("datacenter/%s panel: Prometheus unavailable: %s", label, exc)
        return {
            "source": "unavailable",
            "connected": False,
            "error": str(exc),
            "prometheus_url": base_url(),
            "checked_at": checked_at,
        }
    return {
        "source": "live",
        "connected": True,
        "prometheus_url": base_url(),
        "checked_at": checked_at,
        **data,
    }


@app.get("/api/ui/datacenter/pods")
def ui_dc_pods():
    from tools.prometheus import pod_health

    return _ui_dc_resource(pod_health, "pods")


@app.get("/api/ui/datacenter/workloads")
def ui_dc_workloads():
    from tools.prometheus import workloads

    return _ui_dc_resource(workloads, "workloads")


@app.get("/api/ui/datacenter/nodes")
def ui_dc_nodes():
    from tools.prometheus import nodes

    return _ui_dc_resource(nodes, "nodes")


@app.get("/api/ui/datacenter/storage")
def ui_dc_storage():
    from tools.prometheus import storage

    return _ui_dc_resource(storage, "storage")


def _finding_summary() -> dict:
    """Severity counts over the open findings.

    Shared by /api/ui/alerts and the workbench context panel so the badge and the
    alerts page cannot disagree. Acked findings still count: acknowledging an
    incident does not close it, and this number says how much is outstanding.
    """
    if _db is None or not _db.available:
        return {"open": 0, "critical": 0, "warning": 0, "info": 0, "resolved_recent": 0}

    data = _db.list_findings()
    open_rows = data["open"]
    return {
        "open": len(open_rows),
        "critical": sum(1 for r in open_rows if r["severity"] == "critical"),
        "warning": sum(1 for r in open_rows if r["severity"] == "warning"),
        "info": sum(1 for r in open_rows if r["severity"] == "info"),
        "resolved_recent": len(data["resolved"]),
    }


@app.get("/api/ui/alerts")
def ui_alerts():
    """Open + recently-resolved monitoring findings for the in-app alerts panel."""
    if _db is None or not _db.available:
        return {
            "source": "memory",
            "summary": {"open": 0, "critical": 0, "warning": 0, "info": 0, "resolved_recent": 0},
            "open": [], "resolved": [], "last_check_at": None, "check_no": None,
        }

    from datetime import datetime, timezone

    data = _db.list_findings()
    now = datetime.now(timezone.utc)

    def _row(r) -> dict:
        return {
            "fingerprint": r["fingerprint"],
            "namespace": r["namespace"],
            "kind": r["kind"],
            "resource_name": r["resource_name"],
            "reason": r["reason"],
            "severity": r["severity"],
            "title": r["title"],
            "detail": r["detail"],
            "first_seen": r["first_seen"].isoformat() if r["first_seen"] else None,
            "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            "resolved_at": r["resolved_at"].isoformat() if r["resolved_at"] else None,
            "times_seen": r["times_seen"],
            "acked": bool(r["ack_until"] and r["ack_until"] > now),
        }

    open_rows = [_row(r) for r in data["open"]]
    resolved_rows = [_row(r) for r in data["resolved"]]
    return {
        "source": "live",
        "summary": _finding_summary(),
        "open": open_rows,
        "resolved": resolved_rows,
        "last_check_at": data["last_check_at"].isoformat() if data["last_check_at"] else None,
        "check_no": data["check_no"],
    }


def _live_pending_approvals() -> list[dict]:
    """Interrupted HITL sessions awaiting a human decision, one row per pending call.

    The only real source of pending approvals in the process. demo_data.APPROVALS
    exists so the approvals page has something to render on an empty install; it
    must never be counted as work waiting on an operator.
    """
    live = []
    for s in _sessions.values():
        if s.status != SessionStatus.INTERRUPTED:
            continue
        for action in (s.pending_actions or []):
            name = action.get("name", "变更操作") if isinstance(action, dict) else "变更操作"
            args = action.get("args", {}) if isinstance(action, dict) else {}
            meta = json.dumps(args, ensure_ascii=False)[:80] if args else ""
            live.append({
                "title": name,
                "meta": meta or (s.interrupt_data or "")[:80],
                "applicant": "sre-agent",
                "time": "刚刚",
                "risk": "待评估",
                "risk_level": "warning",
                "expired": False,
                "session_id": s.id,
            })
    return live


@app.get("/api/ui/approvals")
def ui_approvals():
    payload = dict(APPROVALS)
    # Live: surface real interrupted HITL sessions, if any, ahead of the demo rows.
    live = _live_pending_approvals()
    if live:
        payload["items"] = live + payload["items"]
        payload["pending"] = str(len(live))
        payload["source"] = "live"
    return payload


def _cluster_identity() -> dict:
    """Which cluster this process is pointed at.

    Resolved without calling the API server, because the identity is most useful
    exactly when the cluster is unreachable and the panel has to say so.
    """
    if K8S_CONTEXT:
        return {"name": K8S_CONTEXT, "source": "kubeconfig", "in_cluster": IN_CLUSTER}
    if IN_CLUSTER:
        # Kubernetes has no cluster-name API — inside a pod only the deployment
        # knows what it was pointed at, so it has to be supplied.
        return {
            "name": os.getenv("CLUSTER_NAME", "in-cluster"),
            "source": "in_cluster",
            "in_cluster": True,
        }
    try:
        from kubernetes import config as k8s_config

        _, active = k8s_config.list_kube_config_contexts()
        return {
            "name": ((active or {}).get("name") or "unknown"),
            "source": "kubeconfig",
            "in_cluster": False,
        }
    except Exception as exc:  # noqa: BLE001 — a broken kubeconfig must not 500 the panel
        log.warning("could not read kubeconfig context: %s", exc)
        return {"name": "unknown", "source": "unknown", "in_cluster": False}


def _namespace_context() -> dict:
    """Namespaces the agent is told to look at, per agent.py's prompt."""
    from tools.k8s_client import core_v1

    names = sorted(ns.metadata.name for ns in core_v1().list_namespace().items)
    if DEFAULT_NAMESPACES:
        return {
            "total": len(names),
            "watched": [n for n in names if n in DEFAULT_NAMESPACES],
            "explicit": True,
        }
    return {
        "total": len(names),
        "watched": [n for n in names if n not in SYSTEM_NAMESPACES],
        "explicit": False,
    }


@app.get("/api/ui/context")
def ui_context():
    """Real cluster context for the workbench's 当前上下文 panel.

    Deliberately has no demo fallback: this panel tells an operator where the
    next command will land, and a fabricated cluster name is worse than an
    absent one. Alerts and approvals come from state the process already holds
    (Postgres findings, in-process sessions) and stay accurate even when the
    Kubernetes API is unreachable; the namespace list needs the cluster, so only
    that degrades to source="unavailable" — with the cluster name still filled in.
    """
    try:
        namespaces = _namespace_context()
        connected, error = True, None
    except Exception as exc:  # noqa: BLE001 — record the failure, keep the rest
        log.warning("ui context: namespace list failed: %s", exc)
        namespaces = {"total": 0, "watched": [], "explicit": False}
        connected, error = False, str(exc)

    return {
        "source": "live" if connected else "unavailable",
        "connected": connected,
        "error": error,
        "cluster": _cluster_identity(),
        "namespaces": namespaces,
        "alerts": _finding_summary(),
        "approvals": {"pending": len(_live_pending_approvals())},
    }


@app.get("/api/ui/audit")
def ui_audit():
    if _db is not None and _db.available:
        rows = _db.recent_decisions(100)
        if rows:
            return {
                "source": "live",
                "count": len(rows),
                "items": [
                    {
                        "time": (r["ts"].strftime("%Y-%m-%d %H:%M:%S") if r.get("ts") else ""),
                        "action": r.get("decision") or r.get("tool_name") or "操作",
                        "resource": r.get("tool_name") or r.get("session_id") or "",
                        "user": r.get("actor") or "unknown",
                        "result": r.get("result") or "",
                        "result_level": "success",
                    }
                    for r in rows
                ],
            }
    return dict(AUDIT)


# ---------------------------------------------------------------------------
# SkillHub — per-agent skill management API
# ---------------------------------------------------------------------------

def _skillhub_source() -> str:
    return "live" if (_db is not None and _db.available) else "memory"


def _require_agent(agent: str) -> str:
    if agent not in SKILL_AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    return agent


def _require_skill_name(skill: str) -> str:
    if not _SKILL_NAME_RE.match(skill or ""):
        raise HTTPException(
            status_code=400,
            detail="skill_name must be lowercase letters/digits separated by single hyphens",
        )
    return skill


@app.get("/api/skillhub/agents")
def skillhub_agents():
    """List the 10 agents with a per-agent enabled skill count."""
    agents = []
    for name, label in SKILL_AGENTS.items():
        count = 0
        if _db is not None:
            rows = _db.list_skills(name) or []
            count = sum(1 for r in rows if r.get("enabled"))
        agents.append({"name": name, "label": label, "skill_count": count})
    return {"source": _skillhub_source(), "agents": agents}


@app.get("/api/skillhub/{agent}/skills")
def skillhub_list_skills(agent: str):
    _require_agent(agent)
    rows = _db.list_skills(agent) if _db is not None else []
    return {
        "source": _skillhub_source(),
        "agent": agent,
        "skills": rows or [],
    }


@app.post("/api/skillhub/{agent}/skills")
def skillhub_create_skill(agent: str, req: SkillCreateRequest):
    _require_agent(agent)
    name = _require_skill_name(req.skill_name)
    if _db is not None and _db.get_skill(agent, name):
        raise HTTPException(status_code=409, detail=f"Skill already exists: {agent}/{name}")
    skill = _db.create_skill(agent, name, req.description, req.content)
    return {"source": _skillhub_source(), "skill": skill}


@app.get("/api/skillhub/{agent}/{skill}")
def skillhub_get_skill(agent: str, skill: str):
    _require_agent(agent)
    _require_skill_name(skill)
    found = _db.get_skill(agent, skill) if _db is not None else None
    if not found:
        raise HTTPException(status_code=404, detail=f"Skill not found: {agent}/{skill}")
    return {"source": _skillhub_source(), "skill": found}


@app.put("/api/skillhub/{agent}/{skill}")
def skillhub_update_skill(agent: str, skill: str, req: SkillUpdateRequest):
    _require_agent(agent)
    _require_skill_name(skill)
    updated = _db.update_skill(
        agent, skill, description=req.description, content=req.content
    )
    if not updated:
        raise HTTPException(status_code=404, detail=f"Skill not found: {agent}/{skill}")
    return {"source": _skillhub_source(), "skill": updated}


@app.post("/api/skillhub/{agent}/{skill}/toggle")
def skillhub_toggle_skill(agent: str, skill: str, req: SkillToggleRequest):
    _require_agent(agent)
    _require_skill_name(skill)
    ok = _db.set_skill_enabled(agent, skill, req.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Skill not found: {agent}/{skill}")
    return {"source": _skillhub_source(), "enabled": req.enabled}


@app.delete("/api/skillhub/{agent}/{skill}")
def skillhub_delete_skill(agent: str, skill: str):
    _require_agent(agent)
    _require_skill_name(skill)
    ok = _db.delete_skill(agent, skill)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Skill not found: {agent}/{skill}")
    return {"source": _skillhub_source(), "deleted": True}


@app.get("/ui", response_class=RedirectResponse)
def ui_index():
    return RedirectResponse(url="/ui/dashboard.html")


# ---------------------------------------------------------------------------
# Built-in web UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def ui():
    return HTMLResponse(content=_UI_HTML)


_UI_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SRE Bot</title>
<style>
  :root {
    --bg: #0b0e14;
    --bg-elev: #131722;
    --bg-input: #0f131c;
    --border: #232a3b;
    --border-strong: #2f3a55;
    --text: #e6e9f0;
    --text-dim: #8b94a7;
    --accent: #4f8cff;
    --accent-2: #8ab0ff;
    --green: #34d399;
    --amber: #fbbf24;
    --red: #f87171;
    --mono: ui-monospace, 'Cascadia Code', 'JetBrains Mono', 'SF Mono', Consolas, monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, Roboto, sans-serif;
    background: var(--bg);
    background-image:
      radial-gradient(900px circle at 12% -10%, #16203a 0%, transparent 55%),
      radial-gradient(800px circle at 100% 110%, #101b30 0%, transparent 50%);
    color: var(--text);
    height: 100vh; display: flex; flex-direction: column;
    font-size: 14px; -webkit-font-smoothing: antialiased;
  }

  header {
    background: linear-gradient(180deg, #151b2b, #111624);
    border-bottom: 1px solid var(--border);
    padding: 13px 24px;
    display: flex; align-items: center; gap: 14px;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .logo {
    width: 40px; height: 40px; border-radius: 11px; flex: none;
    display: flex; align-items: center; justify-content: center;
    font-size: 21px;
    background: linear-gradient(135deg, #2b6bd8, #4f8cff);
    box-shadow: 0 4px 18px rgba(79, 140, 255, .35);
  }
  .brand h1 { font-size: 17px; font-weight: 650; letter-spacing: .2px; line-height: 1.2; }
  .brand .subtitle { font-size: 11px; color: var(--text-dim); margin-top: 2px; }
  .status-group { display: flex; gap: 8px; margin-left: 6px; }
  .badge {
    display: inline-flex; align-items: center;
    background: var(--bg-elev);
    border: 1px solid var(--border);
    color: var(--green);
    font-size: 11.5px; padding: 4px 11px; border-radius: 999px;
    white-space: nowrap;
  }
  .badge::before {
    content: ''; width: 6px; height: 6px; border-radius: 50%;
    background: currentColor; margin-right: 6px;
    box-shadow: 0 0 6px currentColor;
  }
  #trigger-btn {
    margin-left: auto;
    background: var(--bg-elev); color: var(--text-dim);
    border: 1px solid var(--border-strong); border-radius: 8px;
    padding: 7px 14px; font-size: 12px; cursor: pointer;
    transition: color .15s ease, border-color .15s ease, background .15s ease;
  }
  #trigger-btn:hover { color: var(--accent-2); border-color: var(--accent); background: rgba(79, 140, 255, .08); }

  #chat {
    flex: 1; overflow-y: auto; padding: 28px;
    display: flex; flex-direction: column; gap: 14px;
  }
  #chat::-webkit-scrollbar { width: 10px; }
  #chat::-webkit-scrollbar-track { background: transparent; }
  #chat::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 10px; border: 2px solid var(--bg); }
  #chat::-webkit-scrollbar-thumb:hover { background: #3c4a6b; }

  .msg {
    max-width: 78%; padding: 12px 16px; border-radius: 14px;
    line-height: 1.65; white-space: pre-wrap; font-size: 13.5px;
    animation: fadeIn .25s ease;
  }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

  .msg.user {
    align-self: flex-end;
    background: linear-gradient(135deg, #2b6bd8, #4f8cff);
    color: #fff; border-bottom-right-radius: 4px;
    box-shadow: 0 4px 16px rgba(43, 107, 216, .28);
  }
  .msg.bot {
    align-self: flex-start;
    background: var(--bg-elev); border: 1px solid var(--border);
    border-bottom-left-radius: 4px; color: var(--text);
  }
  .msg.bot.typing { color: var(--text-dim); animation: pulse 1.4s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: .4; } 50% { opacity: 1; } }

  .msg.error { background: rgba(248, 113, 113, .08); border: 1px solid rgba(248, 113, 113, .35); color: #fca5a5; }
  .msg.interrupt {
    background: linear-gradient(180deg, rgba(251, 191, 36, .10), rgba(251, 191, 36, .03));
    border: 1px solid rgba(251, 191, 36, .4); color: #fde68a;
    width: 100%; max-width: 100%;
  }
  .msg.interrupt pre {
    font-family: var(--mono); font-size: 12px;
    background: rgba(0, 0, 0, .25); border-radius: 8px;
    padding: 12px; white-space: pre-wrap; color: var(--text);
  }

  .interrupt-actions { display: flex; gap: 10px; margin-top: 14px; }
  .interrupt-actions button {
    padding: 8px 20px; border-radius: 8px; border: none; cursor: pointer;
    font-size: 13px; font-weight: 600; transition: transform .1s ease, box-shadow .15s ease, background .15s ease;
  }
  .interrupt-actions button:hover { transform: translateY(-1px); }
  .btn-approve { background: linear-gradient(135deg, #1f9d61, #34d399); color: #052e1d; box-shadow: 0 4px 14px rgba(52, 211, 153, .25); }
  .btn-reject { background: rgba(248, 113, 113, .12); color: #fca5a5; border: 1px solid rgba(248, 113, 113, .4); }
  .btn-reject:hover { background: rgba(248, 113, 113, .22); }

  .shortcuts {
    display: flex; gap: 8px; padding: 10px 24px; flex-wrap: wrap;
    border-top: 1px solid var(--border); background: #0e121c;
  }
  .shortcut {
    background: var(--bg-elev); border: 1px solid var(--border);
    color: var(--text-dim); border-radius: 999px; padding: 5px 13px;
    font-size: 12px; cursor: pointer; transition: all .15s ease;
  }
  .shortcut:hover { border-color: var(--accent); color: var(--accent-2); background: rgba(79, 140, 255, .08); transform: translateY(-1px); }

  .todos {
    font-size: 12px; color: var(--text-dim);
    padding: 10px 14px; border-radius: 10px;
    background: var(--bg-elev); border: 1px solid var(--border);
    display: flex; flex-wrap: wrap; gap: 8px;
  }
  .todos span { display: inline-flex; align-items: center; }

  #input-row {
    padding: 16px 24px;
    background: linear-gradient(180deg, #0e121c, #111624);
    border-top: 1px solid var(--border);
    display: flex; gap: 10px; align-items: flex-end;
  }
  #msg-input {
    flex: 1; background: var(--bg-input); border: 1px solid var(--border-strong);
    color: var(--text); border-radius: 12px; padding: 12px 16px; font-size: 14px;
    resize: none; font-family: inherit; line-height: 1.5;
    transition: border-color .15s ease, box-shadow .15s ease;
  }
  #msg-input::placeholder { color: #5a6478; }
  #msg-input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(79, 140, 255, .15); }
  #send-btn {
    background: linear-gradient(135deg, #2b6bd8, #4f8cff); color: #fff;
    border: none; border-radius: 12px; padding: 12px 24px; cursor: pointer;
    font-weight: 600; font-size: 14px;
    box-shadow: 0 4px 14px rgba(43, 107, 216, .3);
    transition: transform .1s ease, box-shadow .15s ease, opacity .15s ease;
  }
  #send-btn:hover:not(:disabled) { transform: translateY(-1px); box-shadow: 0 6px 18px rgba(43, 107, 216, .4); }
  #send-btn:disabled { opacity: .45; cursor: not-allowed; box-shadow: none; }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="logo">☸</div>
    <div>
      <h1>SRE Bot</h1>
      <div class="subtitle">Kubernetes observability &amp; remediation</div>
    </div>
  </div>
  <div class="status-group">
    <span class="badge" id="slack-badge">Slack: checking…</span>
    <span class="badge" id="status-badge">Ready</span>
  </div>
  <button id="trigger-btn" onclick="triggerCheck()">▶ Trigger Check Now</button>
</header>
<div id="chat"></div>
<div class="shortcuts">
  <span class="shortcut" onclick="quickSend('Run a full cluster health audit across all namespaces')">🔍 Full Audit</span>
  <span class="shortcut" onclick="quickSend('Check all pods for issues across all namespaces')">🔴 Pods</span>
  <span class="shortcut" onclick="quickSend('Analyze scaling and HPA configuration')">📈 Scaling</span>
  <span class="shortcut" onclick="quickSend('Analyze CPU and memory performance')">⚡ Performance</span>
  <span class="shortcut" onclick="quickSend('Scan logs for errors and anomalies')">📋 Logs</span>
</div>
<div id="input-row">
  <textarea id="msg-input" rows="2" placeholder="Ask the SRE bot..." onkeydown="onKey(event)"></textarea>
  <button id="send-btn" onclick="sendMessage()">Send</button>
</div>
<script>
let sessionId = null;
let eventSource = null;

fetch('/health').then(r=>r.json()).then(d=>{
  document.getElementById('slack-badge').textContent = 'Slack: ' + (d.slack_enabled ? 'on' : 'off');
  document.getElementById('slack-badge').style.color = d.slack_enabled ? '#68d391' : '#fc8181';
});

function appendMsg(text, cls) {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'msg ' + cls;
  div.textContent = text;
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}
function setStatus(s) {
    const el = document.getElementById('status-badge');
    el.textContent = s;
    el.style.color = s === 'Ready' ? '#34d399' : (s === 'Error' ? '#f87171' : '#fbbf24');
  }
function setInputEnabled(v) {
  document.getElementById('send-btn').disabled = !v;
  document.getElementById('msg-input').disabled = !v;
}
async function sendMessage() {
  const input = document.getElementById('msg-input');
  const text = input.value.trim(); if (!text) return;
  input.value = '';
  appendMsg(text, 'user');
  setInputEnabled(false); setStatus('Running...');
  const typingMsg = appendMsg('Analyzing...', 'bot typing');
  const res = await fetch('/api/chat', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: text, session_id: sessionId}),
  });
  const data = await res.json();
  sessionId = data.session_id;
  typingMsg.remove();
  listenForEvents();
}
function listenForEvents() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/sessions/' + sessionId + '/events');
  eventSource.onmessage = (e) => {
    const ev = JSON.parse(e.data);
    if (ev.type === 'done') {
      appendMsg(ev.data, 'bot'); setStatus('Ready'); setInputEnabled(true); eventSource.close();
    } else if (ev.type === 'interrupt') {
      showInterrupt(ev.data); setStatus('Approval Required'); eventSource.close();
    } else if (ev.type === 'error') {
      appendMsg('Error: ' + ev.data, 'msg error'); setStatus('Error'); setInputEnabled(true); eventSource.close();
    } else if (ev.type === 'todos') {
      showTodos(ev.data);
    }
  };
}
function showInterrupt(data) {
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.className = 'msg interrupt';
  div.innerHTML = '<strong>⚠ Approval Required</strong> <em style="font-size:11px">(also sent to Slack)</em><br><pre style="margin-top:8px;font-size:12px;white-space:pre-wrap">' + data.join('\\n') + '</pre>';
  const actions = document.createElement('div');
  actions.className = 'interrupt-actions';
  const ab = document.createElement('button'); ab.className='btn-approve'; ab.textContent='✓ Approve';
  ab.onclick = () => respond('approve', div);
  const rb = document.createElement('button'); rb.className='btn-reject'; rb.textContent='✗ Reject';
  rb.onclick = () => { const reason = prompt('Reason (optional):') || ''; respond('reject', div, reason); };
  actions.appendChild(ab); actions.appendChild(rb); div.appendChild(actions);
  chat.appendChild(div); chat.scrollTop = chat.scrollHeight;
}
async function respond(decision, div, reason) {
  div.remove(); setStatus('Running...');
  const typingMsg = appendMsg('Continuing...', 'bot typing');
  const body = decision === 'reject' ? {session_id: sessionId, reason: reason||''} : {session_id: sessionId};
  await fetch('/api/' + decision, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  typingMsg.remove(); listenForEvents();
}
function showTodos(todos) {
  const icons = {completed:'✅', in_progress:'🔄', pending:'⏳'};
  const existing = document.getElementById('todos-bar');
  if (existing) existing.remove();
  const chat = document.getElementById('chat');
  const div = document.createElement('div');
  div.id = 'todos-bar'; div.className = 'todos';
  div.innerHTML = todos.map(t => `<span>${icons[t.status]||'•'} ${t.content}</span>`).join('');
  chat.appendChild(div); chat.scrollTop = chat.scrollHeight;
}
async function triggerCheck() {
  appendMsg('Triggering immediate health check...', 'bot');
  await fetch('/api/trigger-check', {method:'POST'});
  appendMsg('Health check started. Results will appear in Slack.', 'bot');
}
function quickSend(text) { document.getElementById('msg-input').value = text; sendMessage(); }
function onKey(e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } }
</script>
</body>
</html>
"""

# Static ops console (mounted after all routes so the /ui redirect route above
# wins for the exact /ui path; everything else falls through to the static files).
import os as _os

_UI_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "sre-agent-ui", "pages")
if _os.path.isdir(_UI_DIR):
    app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")


if __name__ == "__main__":
    import uvicorn
    from config import API_PORT
    uvicorn.run("api:app", host="0.0.0.0", port=API_PORT, reload=False)
