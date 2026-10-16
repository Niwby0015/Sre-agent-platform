"""Durable state: langgraph checkpoints, sessions, HITL audit, monitoring state.

Everything the bot needed to survive a restart used to live in process memory —
``MemorySaver``, ``InMemoryStore``, and ``api._sessions``. A pod replacement
orphaned every pending HITL approval: the Slack Approve button stayed live but
the session behind it was gone, so the click dead-ended and the proposed cluster
change could neither be applied nor properly rejected.

This module backs all of that with Postgres.

Degradation is deliberate. If ``DATABASE_URL`` is unset (local CLI use) or the
database is unreachable at startup, the process falls back to in-memory
equivalents and logs loudly rather than refusing to boot. The bot runs *inside*
the cluster hosting its own database, so a cluster problem must not also take
away the operator's ability to ask the bot about it. ``/health`` reports which
mode is live so the degradation is never silent.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from monitor_state import ReportDiff, StoredFinding

log = logging.getLogger("sre-agent.persistence")


SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id                text PRIMARY KEY,
    thread_id         text NOT NULL,
    status            text NOT NULL,
    source            text NOT NULL DEFAULT 'api',
    pending_decisions integer NOT NULL DEFAULT 1,
    pending_actions   jsonb,
    interrupt_data    jsonb,
    last_response     text,
    title             text,
    slack_message_ts  text,
    slack_channel     text,
    slack_thread_ts   text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- Append-only. Nothing in this module issues UPDATE or DELETE against it: the
-- point is an immutable record of who authorised each cluster mutation.
CREATE TABLE IF NOT EXISTS hitl_audit (
    id         bigserial PRIMARY KEY,
    ts         timestamptz NOT NULL DEFAULT now(),
    session_id text NOT NULL,
    thread_id  text,
    actor      text,
    actor_id   text,
    decision   text NOT NULL,
    source     text,
    tool_name  text,
    tool_args  jsonb,
    result     text
);
CREATE INDEX IF NOT EXISTS hitl_audit_session_idx ON hitl_audit (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS hitl_audit_ts_idx ON hitl_audit (ts DESC);

CREATE TABLE IF NOT EXISTS finding_state (
    fingerprint   text PRIMARY KEY,
    namespace     text NOT NULL DEFAULT '',
    kind          text NOT NULL DEFAULT '',
    resource_name text NOT NULL DEFAULT '',
    reason        text NOT NULL DEFAULT '',
    severity      text NOT NULL,
    title         text NOT NULL DEFAULT '',
    detail        text NOT NULL DEFAULT '',
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    times_seen    integer NOT NULL DEFAULT 1,
    resolved_at   timestamptz,
    ack_until     timestamptz
);
CREATE INDEX IF NOT EXISTS finding_state_open_idx
    ON finding_state (last_seen DESC) WHERE resolved_at IS NULL;

-- Maps a posted report to the fingerprints it covered, so the Slack "Ack"
-- button can carry a short opaque id instead of a fingerprint list that would
-- blow past Slack's 2000-character button value limit.
CREATE TABLE IF NOT EXISTS monitor_reports (
    report_id    text PRIMARY KEY,
    created_at   timestamptz NOT NULL DEFAULT now(),
    fingerprints jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS monitor_meta (
    key   text PRIMARY KEY,
    value text
);

-- Per-agent skills (SkillHub). One shared table; ``agent_name`` is the
-- discriminator ('main' | 'pod-inspector' | ...). ``content`` is the SKILL.md
-- body (frontmatter not stored — ``description`` is held separately). ``enabled``
-- is the runtime on/off switch the middleware reads each run.
CREATE TABLE IF NOT EXISTS skills (
    id          bigserial PRIMARY KEY,
    agent_name  text NOT NULL,
    skill_name  text NOT NULL,
    description text NOT NULL DEFAULT '',
    content     text NOT NULL DEFAULT '',
    enabled     boolean NOT NULL DEFAULT true,
    version     integer NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (agent_name, skill_name)
);

-- Append-only version history. Cascade-deleted with its skill.
CREATE TABLE IF NOT EXISTS skill_versions (
    id          bigserial PRIMARY KEY,
    skill_id    bigint NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    version     integer NOT NULL,
    description text,
    content     text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
"""

# How far back a resolved finding is still remembered. Keeps flap detection
# working ("this came back for the 4th time this week") while bounding the table.
_RESOLVED_RETENTION_DAYS = 7

# In-memory skill store backing NullDatabase's SkillHub methods. Non-persistent —
# mirrors how api._sessions lives only for the process lifetime. Keyed by
# (agent_name, skill_name).
_MEMORY_SKILLS: dict[tuple[str, str], dict] = {}


class NullDatabase:
    """No-op implementation used when Postgres is unavailable.

    Same surface as :class:`PostgresDatabase` so callers never branch on
    ``if db:``. Monitoring degrades to its old stateless behaviour (every run
    looks new) and the audit log is dropped — hence the startup warning.
    """

    kind = "memory"
    available = False

    def setup(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:  # pragma: no cover - trivial
        pass

    def save_session(self, session: dict) -> None:
        pass

    def load_session(self, session_id: str) -> Optional[dict]:
        return None

    def list_sessions(self, limit: int = 50) -> list[dict]:
        return []

    def record_decision(self, **kwargs) -> None:
        log.info(
            "[NO AUDIT DB] HITL %s on session=%s by actor=%s tool=%s",
            kwargs.get("decision"), kwargs.get("session_id"),
            kwargs.get("actor"), kwargs.get("tool_name"),
        )

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        return []

    def load_tracked_findings(self) -> dict[str, StoredFinding]:
        return {}

    def apply_diff(self, diff: ReportDiff, now: Optional[datetime] = None) -> None:
        pass

    def save_report(self, fingerprints: list[str]) -> Optional[str]:
        return None

    def ack_report(self, report_id: str, hours: int) -> int:
        return 0

    def next_check_number(self) -> int:
        return 0

    def list_findings(self) -> dict:
        return {"open": [], "resolved": [], "last_check_at": None, "check_no": None}

    # -- skills (in-memory SkillHub fallback) ------------------------------

    def list_skills(self, agent_name: str) -> list[dict]:
        rows = [
            {k: v for k, v in rec.items() if k != "content"}
            for (a, _), rec in _MEMORY_SKILLS.items()
            if a == agent_name
        ]
        rows.sort(key=lambda r: r["skill_name"])
        return rows

    def get_skill(self, agent_name: str, skill_name: str) -> Optional[dict]:
        return _MEMORY_SKILLS.get((agent_name, skill_name))

    def create_skill(self, agent_name: str, skill_name: str, description: str, content: str) -> dict:
        now = datetime.now(timezone.utc)
        rec = {
            "agent_name": agent_name,
            "skill_name": skill_name,
            "description": description,
            "content": content,
            "enabled": True,
            "version": 1,
            "created_at": now,
            "updated_at": now,
        }
        _MEMORY_SKILLS[(agent_name, skill_name)] = rec
        return rec

    def update_skill(
        self,
        agent_name: str,
        skill_name: str,
        description: Optional[str] = None,
        content: Optional[str] = None,
    ) -> Optional[dict]:
        rec = _MEMORY_SKILLS.get((agent_name, skill_name))
        if rec is None:
            return None
        if description is not None:
            rec["description"] = description
        if content is not None:
            rec["content"] = content
        rec["version"] += 1
        rec["updated_at"] = datetime.now(timezone.utc)
        return rec

    def set_skill_enabled(self, agent_name: str, skill_name: str, enabled: bool) -> bool:
        rec = _MEMORY_SKILLS.get((agent_name, skill_name))
        if rec is None:
            return False
        rec["enabled"] = bool(enabled)
        rec["updated_at"] = datetime.now(timezone.utc)
        return True

    def delete_skill(self, agent_name: str, skill_name: str) -> bool:
        return _MEMORY_SKILLS.pop((agent_name, skill_name), None) is not None

    def query_enabled_skills(self, agent_name: str) -> list[dict]:
        rows = [
            {
                "skill_name": rec["skill_name"],
                "description": rec["description"],
                "content": rec["content"],
            }
            for (a, _), rec in _MEMORY_SKILLS.items()
            if a == agent_name and rec["enabled"]
        ]
        rows.sort(key=lambda r: r["skill_name"])
        return rows


class PostgresDatabase:
    """Postgres-backed state. All SQL is parameterized; no value interpolation."""

    kind = "postgres"
    available = True

    def __init__(self, pool):
        self._pool = pool

    def setup(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(SCHEMA)
            # CREATE TABLE IF NOT EXISTS won't add a column to an existing table;
            # migrate earlier deployments that lack the newer `title` column.
            conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS title text")
        log.info("Postgres schema ready (sessions, hitl_audit, finding_state, monitor_*, skills)")

    def close(self) -> None:
        try:
            self._pool.close()
        except Exception:  # pragma: no cover - shutdown best effort
            log.debug("Connection pool close failed", exc_info=True)

    # -- sessions ---------------------------------------------------------

    def save_session(self, session: dict) -> None:
        """Write-through upsert of a session's durable fields."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, thread_id, status, source, pending_decisions,
                    pending_actions, interrupt_data, last_response, title,
                    slack_message_ts, slack_channel, slack_thread_ts, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    thread_id        = EXCLUDED.thread_id,
                    status           = EXCLUDED.status,
                    source           = EXCLUDED.source,
                    pending_decisions= EXCLUDED.pending_decisions,
                    pending_actions  = EXCLUDED.pending_actions,
                    interrupt_data   = EXCLUDED.interrupt_data,
                    last_response    = EXCLUDED.last_response,
                    title            = EXCLUDED.title,
                    slack_message_ts = EXCLUDED.slack_message_ts,
                    slack_channel    = EXCLUDED.slack_channel,
                    slack_thread_ts  = EXCLUDED.slack_thread_ts,
                    updated_at       = now()
                """,
                (
                    session["id"],
                    session["thread_id"],
                    session["status"],
                    session.get("source", "api"),
                    session.get("pending_decisions", 1),
                    Jsonb(session.get("pending_actions") or []),
                    Jsonb(session.get("interrupt_data") or []),
                    session.get("last_response", ""),
                    session.get("title", ""),
                    session.get("slack_message_ts"),
                    session.get("slack_channel"),
                    session.get("slack_thread_ts"),
                ),
            )

    def load_session(self, session_id: str) -> Optional[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT id, thread_id, status, source, pending_decisions,
                       pending_actions, interrupt_data, last_response, title,
                       slack_message_ts, slack_channel, slack_thread_ts
                  FROM sessions
                 WHERE id = %s
                """,
                (session_id,),
            )
            return cur.fetchone()

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """Recent sessions, newest first, for the workbench history sidebar."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT id, thread_id, title, status, source, last_response,
                       created_at, updated_at
                  FROM sessions
                 ORDER BY updated_at DESC
                 LIMIT %s
                """,
                (min(max(limit, 1), 200),),
            )
            return list(cur.fetchall())

    # -- audit ------------------------------------------------------------

    def record_decision(
        self,
        session_id: str,
        decision: str,
        thread_id: str = "",
        actor: str = "",
        actor_id: str = "",
        source: str = "",
        tool_name: str = "",
        tool_args: Any = None,
        result: str = "",
    ) -> None:
        """Append one immutable HITL decision record."""
        from psycopg.types.json import Jsonb

        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO hitl_audit (
                    session_id, thread_id, actor, actor_id, decision,
                    source, tool_name, tool_args, result
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    session_id, thread_id, actor, actor_id, decision,
                    source, tool_name, Jsonb(tool_args or {}), (result or "")[:4000],
                ),
            )

    def recent_decisions(self, limit: int = 50) -> list[dict]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT ts, session_id, actor, actor_id, decision, source,
                       tool_name, tool_args, result
                  FROM hitl_audit
                 ORDER BY ts DESC
                 LIMIT %s
                """,
                (min(max(limit, 1), 500),),
            )
            return list(cur.fetchall())

    # -- monitoring finding state -----------------------------------------

    def load_tracked_findings(self) -> dict[str, StoredFinding]:
        """Open findings, plus recently-resolved and acked ones.

        Recently-resolved rows are included so a returning problem is reported
        as new again while keeping its cumulative ``times_seen``.
        """
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT fingerprint, severity, title, namespace, first_seen,
                       last_seen, times_seen, resolved_at, ack_until
                  FROM finding_state
                 WHERE resolved_at IS NULL
                    OR resolved_at > now() - make_interval(days => %s)
                    OR ack_until > now()
                """,
                (_RESOLVED_RETENTION_DAYS,),
            )
            rows = cur.fetchall()

        return {
            r["fingerprint"]: StoredFinding(
                fingerprint=r["fingerprint"],
                severity=r["severity"],
                title=r["title"],
                namespace=r["namespace"],
                first_seen=r["first_seen"],
                last_seen=r["last_seen"],
                times_seen=r["times_seen"],
                resolved_at=r["resolved_at"],
                ack_until=r["ack_until"],
            )
            for r in rows
        }

    def list_findings(self) -> dict:
        """Open + recently-resolved findings with full detail, for the alerts panel."""
        cols = (
            "fingerprint, namespace, kind, resource_name, reason, "
            "severity, title, detail, first_seen, last_seen, "
            "times_seen, resolved_at, ack_until"
        )
        with self._pool.connection() as conn:
            open_rows = conn.execute(
                f"""
                SELECT {cols}
                  FROM finding_state
                 WHERE resolved_at IS NULL
                 ORDER BY CASE severity
                            WHEN 'critical' THEN 0
                            WHEN 'warning' THEN 1
                            ELSE 2
                          END, last_seen DESC
                """
            ).fetchall()
            resolved_rows = conn.execute(
                f"""
                SELECT {cols}
                  FROM finding_state
                 WHERE resolved_at IS NOT NULL
                 ORDER BY resolved_at DESC
                 LIMIT 50
                """
            ).fetchall()
            last_report = conn.execute(
                "SELECT max(created_at) AS last FROM monitor_reports"
            ).fetchone()
            check_no = conn.execute(
                "SELECT value FROM monitor_meta WHERE key = 'check_count'"
            ).fetchone()
        return {
            "open": open_rows,
            "resolved": resolved_rows,
            "last_check_at": last_report["last"] if last_report else None,
            "check_no": check_no["value"] if check_no else None,
        }

    def apply_diff(self, diff: ReportDiff, now: Optional[datetime] = None) -> None:
        """Persist the outcome of one monitoring run, atomically."""
        now = now or datetime.now(timezone.utc)
        seen = diff.active + diff.suppressed

        with self._pool.connection() as conn:
            with conn.transaction():
                for delta in seen:
                    f = delta.finding
                    conn.execute(
                        """
                        INSERT INTO finding_state (
                            fingerprint, namespace, kind, resource_name, reason,
                            severity, title, detail, first_seen, last_seen,
                            times_seen, resolved_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL)
                        ON CONFLICT (fingerprint) DO UPDATE SET
                            namespace     = EXCLUDED.namespace,
                            kind          = EXCLUDED.kind,
                            resource_name = EXCLUDED.resource_name,
                            reason        = EXCLUDED.reason,
                            severity      = EXCLUDED.severity,
                            title         = EXCLUDED.title,
                            detail        = EXCLUDED.detail,
                            last_seen     = EXCLUDED.last_seen,
                            times_seen    = EXCLUDED.times_seen,
                            resolved_at   = NULL,
                            -- A finding that had been resolved and came back
                            -- restarts its clock; one that never closed keeps
                            -- the earliest first_seen we know about.
                            first_seen    = CASE
                                WHEN finding_state.resolved_at IS NOT NULL
                                    THEN EXCLUDED.first_seen
                                ELSE LEAST(finding_state.first_seen, EXCLUDED.first_seen)
                            END
                        """,
                        (
                            delta.fingerprint,
                            getattr(f, "namespace", "") or "",
                            getattr(f, "kind", "") or "",
                            getattr(f, "resource_name", "") or "",
                            getattr(f, "reason", "") or "",
                            f.severity,
                            (getattr(f, "title", "") or "")[:500],
                            (getattr(f, "detail", "") or "")[:4000],
                            delta.first_seen,
                            now,
                            delta.times_seen,
                        ),
                    )

                if diff.resolved:
                    conn.execute(
                        """
                        UPDATE finding_state
                           SET resolved_at = %s
                         WHERE fingerprint = ANY(%s)
                        """,
                        (now, [r.fingerprint for r in diff.resolved]),
                    )

    def save_report(self, fingerprints: list[str]) -> Optional[str]:
        """Record which fingerprints a posted report covered; return its id."""
        from psycopg.types.json import Jsonb

        report_id = uuid.uuid4().hex[:12]
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO monitor_reports (report_id, fingerprints) VALUES (%s, %s)",
                (report_id, Jsonb(list(fingerprints))),
            )
        return report_id

    def ack_report(self, report_id: str, hours: int) -> int:
        """Suppress every finding in a report for ``hours``. Returns rows acked."""
        until = datetime.now(timezone.utc) + timedelta(hours=hours)
        with self._pool.connection() as conn:
            cur = conn.execute(
                "SELECT fingerprints FROM monitor_reports WHERE report_id = %s",
                (report_id,),
            )
            row = cur.fetchone()
            if not row:
                return 0
            fingerprints = list(row["fingerprints"] or [])
            if not fingerprints:
                return 0
            cur = conn.execute(
                """
                UPDATE finding_state
                   SET ack_until = %s
                 WHERE fingerprint = ANY(%s)
                   AND resolved_at IS NULL
                """,
                (until, fingerprints),
            )
            return cur.rowcount

    # -- meta -------------------------------------------------------------

    def next_check_number(self) -> int:
        """Monotonic counter of completed checks, used to pace the digest."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO monitor_meta (key, value) VALUES ('check_count', '1')
                ON CONFLICT (key) DO UPDATE
                    SET value = (COALESCE(monitor_meta.value::bigint, 0) + 1)::text
                RETURNING value
                """
            )
            row = cur.fetchone()
        try:
            return int(row["value"])
        except (TypeError, ValueError, KeyError):
            return 0

    # -- skills (SkillHub) ------------------------------------------------

    def list_skills(self, agent_name: str) -> list[dict]:
        """Skill summaries (no content) for one agent, ordered by name."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT agent_name, skill_name, description, enabled, version,
                       created_at, updated_at
                  FROM skills
                 WHERE agent_name = %s
                 ORDER BY skill_name
                """,
                (agent_name,),
            )
            return list(cur.fetchall())

    def get_skill(self, agent_name: str, skill_name: str) -> Optional[dict]:
        """One skill's full record (including content), or None."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT agent_name, skill_name, description, content, enabled,
                       version, created_at, updated_at
                  FROM skills
                 WHERE agent_name = %s AND skill_name = %s
                """,
                (agent_name, skill_name),
            )
            return cur.fetchone()

    def create_skill(
        self, agent_name: str, skill_name: str, description: str, content: str
    ) -> dict:
        """Insert a skill plus its version-1 history row, atomically.

        Raises a unique-violation if (agent_name, skill_name) already exists;
        callers pre-check with :meth:`get_skill`.
        """
        with self._pool.connection() as conn:
            with conn.transaction():
                row = conn.execute(
                    """
                    INSERT INTO skills (agent_name, skill_name, description, content)
                    VALUES (%s, %s, %s, %s)
                    RETURNING agent_name, skill_name, description, content,
                              enabled, version, created_at, updated_at
                    """,
                    (agent_name, skill_name, description, content),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO skill_versions (skill_id, version, description, content)
                    SELECT id, version, description, content FROM skills
                     WHERE agent_name = %s AND skill_name = %s
                    """,
                    (agent_name, skill_name),
                )
        return row

    def update_skill(
        self,
        agent_name: str,
        skill_name: str,
        description: Optional[str] = None,
        content: Optional[str] = None,
    ) -> Optional[dict]:
        """Update description/content, bump the version, append history. Atomic."""
        with self._pool.connection() as conn:
            with conn.transaction():
                cur = conn.execute(
                    """
                    SELECT id, description, content, version
                      FROM skills
                     WHERE agent_name = %s AND skill_name = %s
                     FOR UPDATE
                    """,
                    (agent_name, skill_name),
                )
                existing = cur.fetchone()
                if existing is None:
                    return None
                new_desc = existing["description"] if description is None else description
                new_content = existing["content"] if content is None else content
                new_version = existing["version"] + 1
                row = conn.execute(
                    """
                    UPDATE skills
                       SET description = %s, content = %s, version = %s,
                           updated_at = now()
                     WHERE id = %s
                    RETURNING agent_name, skill_name, description, content,
                              enabled, version, created_at, updated_at
                    """,
                    (new_desc, new_content, new_version, existing["id"]),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO skill_versions (skill_id, version, description, content)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (existing["id"], new_version, new_desc, new_content),
                )
        return row

    def set_skill_enabled(self, agent_name: str, skill_name: str, enabled: bool) -> bool:
        """Flip the enabled switch. Returns True if a row changed."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                UPDATE skills
                   SET enabled = %s, updated_at = now()
                 WHERE agent_name = %s AND skill_name = %s
                """,
                (bool(enabled), agent_name, skill_name),
            )
            return cur.rowcount > 0

    def delete_skill(self, agent_name: str, skill_name: str) -> bool:
        """Delete a skill (its version rows cascade). Returns True if removed."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM skills WHERE agent_name = %s AND skill_name = %s",
                (agent_name, skill_name),
            )
            return cur.rowcount > 0

    def query_enabled_skills(self, agent_name: str) -> list[dict]:
        """Enabled skills (name/description/content) for the middleware's read path."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """
                SELECT skill_name, description, content
                  FROM skills
                 WHERE agent_name = %s AND enabled = true
                 ORDER BY skill_name
                """,
                (agent_name,),
            )
            return list(cur.fetchall())


def init_persistence(database_url: str = "") -> tuple[Any, Any, Any]:
    """Build ``(checkpointer, store, database)``.

    Falls back to in-memory implementations when no DSN is configured or the
    database cannot be reached, so the CLI and a degraded cluster both still work.
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.store.memory import InMemoryStore

    if not database_url:
        log.warning(
            "DATABASE_URL not set — using in-memory checkpointer/store. Pending HITL "
            "approvals and monitoring finding-state will NOT survive a restart."
        )
        return MemorySaver(), InMemoryStore(), NullDatabase()

    pool = None
    try:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore

        pool = ConnectionPool(
            conninfo=database_url,
            min_size=1,
            max_size=10,
            open=True,
            # PostgresSaver requires both of these on every connection.
            kwargs={"autocommit": True, "row_factory": dict_row},
        )

        # PostgresSaver implements only the synchronous checkpoint API. langgraph's
        # ``astream``/``ainvoke`` call the async ``aget_tuple``/``aput``/... which the
        # base class leaves as NotImplementedError. Bridge them over the sync methods
        # via a worker thread so the same sync pool backs both ``astream`` (web/SSE)
        # and ``invoke`` (Slack) call paths — the sync hooks stay inherited unchanged.
        class _AsyncBridgePostgresSaver(PostgresSaver):
            async def aget_tuple(self, config):
                return await asyncio.to_thread(self.get_tuple, config)

            async def aget(self, config):
                return await asyncio.to_thread(self.get, config)

            async def aput(self, config, checkpoint, metadata, new_versions):
                return await asyncio.to_thread(
                    self.put, config, checkpoint, metadata, new_versions
                )

            async def aput_writes(self, config, writes, task_id, task_path=""):
                return await asyncio.to_thread(
                    self.put_writes, config, writes, task_id, task_path
                )

            async def adelete_thread(self, thread_id):
                return await asyncio.to_thread(self.delete_thread, thread_id)

            async def adelete_for_runs(self, run_ids):
                return await asyncio.to_thread(self.delete_for_runs, run_ids)

            async def acopy_thread(self, source_thread_id, target_thread_id):
                return await asyncio.to_thread(
                    self.copy_thread, source_thread_id, target_thread_id
                )

            async def aprune(self, thread_ids, *, strategy="keep_latest"):
                return await asyncio.to_thread(self.prune, thread_ids, strategy=strategy)

            async def aget_delta_channel_history(self, *, config, channels):
                return await asyncio.to_thread(
                    self.get_delta_channel_history, config=config, channels=channels
                )

            async def alist(self, config, *, filter=None, before=None, limit=None):
                def _collect():
                    return list(self.list(config, filter=filter, before=before, limit=limit))

                for item in await asyncio.to_thread(_collect):
                    yield item

        checkpointer = _AsyncBridgePostgresSaver(pool)
        checkpointer.setup()

        store = PostgresStore(pool)
        store.setup()

        db = PostgresDatabase(pool)
        db.setup()

        log.info("Persistence ready (postgres)")
        return checkpointer, store, db
    except Exception:
        log.exception(
            "Postgres unavailable — DEGRADED: falling back to in-memory state. "
            "HITL approvals will not survive a restart and no audit trail is being written."
        )
        # An opened-but-unusable pool keeps background worker threads retrying
        # the dead DSN for the life of the process. Close it before degrading.
        if pool is not None:
            try:
                pool.close()
            except Exception:
                log.debug("Failed to close half-open pool", exc_info=True)
        return MemorySaver(), InMemoryStore(), NullDatabase()
