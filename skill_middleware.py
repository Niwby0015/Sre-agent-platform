"""DB-backed skill injection middleware + read_skill tool.

This is the "pluggable" seam between the SkillHub CRUD store and deepagents.
It uses progressive disclosure: only each enabled skill's name + description is
inlined into the system prompt; the model reads the full body on demand via the
`read_skill` tool. Enabling, disabling, adding, or editing a skill in the
SkillHub UI changes what the next run sees — no code change, no agent rebuild.
"""
from __future__ import annotations

import logging

from deepagents.middleware._utils import append_to_system_message
from deepagents.middleware.skills import SkillsMiddleware
from langchain.tools import tool

from config import SKILL_INJECT_MAX_CHARS

log = logging.getLogger("sre-agent.skill-middleware")


def make_read_skill_tool(db, agent_name: str):
    """Build a `read_skill` tool bound to one agent's SkillHub store.

    Each agent (main + every subagent) gets its own instance, so `read_skill`
    only ever exposes that agent's own skills. The closure captures `db` and
    `agent_name`; the model supplies just `skill_name`.
    """

    @tool
    def read_skill(skill_name: str) -> str:
        """Read the full instructions of one of this agent's skills.

        Call this only after you see a skill in your "## Skills" section whose
        name or description looks relevant to the current task. Pass the exact
        skill name. Returns the skill's full step-by-step content.
        """
        name = (skill_name or "").strip()
        if not name:
            return "ERROR: read_skill requires a skill name."
        skill = db.get_skill(agent_name, name) if db is not None else None
        if not skill:
            return (
                f"ERROR: no skill named '{name}' for agent '{agent_name}'. "
                "Choose a name from the '## Skills' section of your system "
                "prompt and retry."
            )
        content = (skill.get("content") or "").strip()
        if not content:
            return f"Skill '{name}' has no content yet."
        if len(content) > SKILL_INJECT_MAX_CHARS:
            content = content[:SKILL_INJECT_MAX_CHARS].rstrip() + "\n\n[content truncated]"
        return content

    return read_skill


class DBSkillsMiddleware(SkillsMiddleware):
    """Inject an agent's enabled skills (metadata only) into its system prompt."""

    def __init__(self, db, agent_name: str) -> None:
        self._db = db
        self._agent_name = agent_name

    def _load(self) -> dict:
        rows = []
        if self._db is not None:
            rows = self._db.query_enabled_skills(self._agent_name) or []
        return {"skills_metadata": rows}

    def before_agent(self, state, runtime, config):
        # Always reload, unlike the parent which loads once per session, so a
        # SkillHub toggle applies on the very next run.
        return self._load()

    async def abefore_agent(self, state, runtime, config):
        return self._load()

    def modify_request(self, request):
        skills = request.state.get("skills_metadata", []) or []
        if not skills:
            return request

        lines = []
        for s in skills:
            name = (s.get("skill_name") or s.get("name") or "").strip()
            desc = (s.get("description") or "").strip()
            if not name:
                continue
            lines.append(f"- **{name}** — {desc}" if desc else f"- **{name}**")

        if not lines:
            return request

        block = (
            "## Skills\n\n"
            "You have the following skills available. A skill is a short, reusable "
            "procedure or body of domain knowledge. When one looks relevant to the "
            "current task, read it with the `read_skill` tool first, then follow its "
            "steps. Use your own judgment — a skill is guidance, not an override of "
            "live cluster evidence or the operator's explicit instructions.\n\n"
            + "\n".join(lines)
        )
        return request.override(
            system_message=append_to_system_message(request.system_message, block)
        )
