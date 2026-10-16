import os
from dotenv import load_dotenv

load_dotenv()

# Model ids are overridable so an evaluation run can pin — and record — exactly
# which model produced a score. The defaults are what production uses.
MODEL = os.getenv("AGENT_MODEL", "claude-sonnet-4-6")
SUBAGENT_MODEL = os.getenv("SUBAGENT_MODEL", "claude-haiku-4-5")  # read-only subagents
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "")

# ---------------------------------------------------------------------------
# RAG embeddings
# ---------------------------------------------------------------------------
# Multilingual E5 keeps Chinese operator queries compatible with English
# runbooks while fitting local Windows development machines. E5-small uses 384 dims.
RAG_EMBEDDING_MODEL = os.getenv("RAG_EMBEDDING_MODEL", "intfloat/multilingual-e5-small")
RAG_EMBEDDING_DIMENSIONS = int(os.getenv("RAG_EMBEDDING_DIMENSIONS", "384"))
RAG_EMBEDDING_DEVICE = os.getenv("RAG_EMBEDDING_DEVICE", "auto")
RAG_TOP_K = int(os.getenv("RAG_TOP_K", "4"))
RAG_MIN_SCORE = float(os.getenv("RAG_MIN_SCORE", "0.65"))
RAG_QDRANT_URL = os.getenv("RAG_QDRANT_URL", "http://localhost:6333")
RAG_QDRANT_API_KEY = os.getenv("RAG_QDRANT_API_KEY", "")
RAG_QDRANT_COLLECTION = os.getenv(
    "RAG_QDRANT_COLLECTION", "sre_runbooks_multilingual_e5_small_v1"
)

# In-cluster detection: Kubernetes injects SERVICE_ACCOUNT_TOKEN at this path
IN_CLUSTER = os.path.exists("/var/run/secrets/kubernetes.io/serviceaccount/token")

# For local dev: optionally pin to a specific context / namespace list
K8S_CONTEXT = os.getenv("K8S_CONTEXT", "")

# Infrastructure rather than workload. The scheduler skips these when listing
# deployments, and the agent prompt tells the model to auto-discover everything
# else — one definition so the two cannot drift apart.
SYSTEM_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease")

DEFAULT_NAMESPACES = [
    ns.strip() for ns in os.getenv("DEFAULT_NAMESPACES", "").split(",") if ns.strip()
]

API_PORT = int(os.getenv("API_PORT", "8080"))

# Browser origins allowed to call /api/*. Defaults to EMPTY, meaning no
# cross-origin access. The built-in web UI is served by this same app at "/", so
# its requests are same-origin and need no grant. A previous "*" meant any page
# the operator visited while `kubectl port-forward` was open could read
# /api/audit, which exposes the arguments of approved cluster mutations.
CORS_ALLOW_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()
]

# ---------------------------------------------------------------------------
# Cost / runaway-loop safety limits
# ---------------------------------------------------------------------------
# A langgraph "super-step" cap. This is the hard backstop that guarantees the
# graph cannot loop forever (e.g. the agent grep/read_file-ing files in a
# cycle). Applied to every agent.invoke via make_agent_config().
#
# NOTE: the orchestrator (deepagents `create_deep_agent`) fans out to 9
# subagents via the `task` tool, and those subagents INHERIT this limit
# (deepagents invokes them with no explicit override, so the parent config's
# recursion_limit propagates). The old default of 60 was too low: a single
# thorough health audit easily blows past 60 super-steps and surfaced a bogus
# "Recursion limit reached" error to the frontend. deepagents itself defaults
# to 9_999 and relies on model/tool-call middleware as the real backstop, so
# we raise this to a level that admits legitimate fan-out while still bounding
# runaway subagent loops (which do NOT inherit the parent's model/tool-call
# limits below).
RECURSION_LIMIT = int(os.getenv("AGENT_RECURSION_LIMIT", "300"))

# Max model (LLM) calls. run_limit = per single invocation; thread_limit =
# cumulative across a conversation thread. Backstop against runaway token spend.
MODEL_CALL_RUN_LIMIT = int(os.getenv("MODEL_CALL_RUN_LIMIT", "40"))
MODEL_CALL_THREAD_LIMIT = int(os.getenv("MODEL_CALL_THREAD_LIMIT", "120"))

# Max total tool calls in a single run, and a tighter cap on the read-heavy
# filesystem tools that caused the original grep/read_file cost incident.
TOOL_CALL_RUN_LIMIT = int(os.getenv("TOOL_CALL_RUN_LIMIT", "80"))
FS_TOOL_RUN_LIMIT = int(os.getenv("FS_TOOL_RUN_LIMIT", "25"))

# Max search_knowledge (Runbook retrieval) calls per run. Retrieval is meant to
# fire once per distinct incident type, so this is a hard backstop against the
# agent re-searching the same runbook in a loop.
RAG_SEARCH_RUN_LIMIT = int(os.getenv("RAG_SEARCH_RUN_LIMIT", "8"))

# Max Prometheus query calls per run. Unlike the filesystem and Runbook caps,
# this one exists for two reasons: a PromQL loop is a load generator against a
# service that also serves every other dashboard, and each result is a time
# series that costs context. Chosen against the typical shape of a metrics
# investigation (a few node-level queries plus one per suspect workload).
PROMETHEUS_RUN_LIMIT = int(os.getenv("PROMETHEUS_RUN_LIMIT", "10"))

# Widest window prometheus_query_range will serve, in minutes. The model passes
# a window in minutes; this clamps it, so a request for "the last year" cannot
# turn into a scan that returns before the heat death of the cluster.
PROMETHEUS_MAX_RANGE_MINUTES = int(os.getenv("PROMETHEUS_MAX_RANGE_MINUTES", "1440"))

# Most time series any single Prometheus result may return. Past this the tool
# reports the top N by peak value and says how many it withheld — a query like
# container_cpu_usage_seconds_total matches every container in the cluster, and
# pasting all of them into the transcript crowds out the evidence that matters.
PROMETHEUS_MAX_SERIES = int(os.getenv("PROMETHEUS_MAX_SERIES", "20"))

# Points per series in a range result. Bounds both the payload and the token
# cost; 120 is enough to read a trend and small enough to stay cheap.
PROMETHEUS_MAX_POINTS = int(os.getenv("PROMETHEUS_MAX_POINTS", "120"))

# Max characters any single tool result may contribute to the message history.
# Every other limit in this file counts CALLS; none bounded bytes, and that is how
# a run reached a 632,740-token prompt against a 200,000 ceiling while satisfying
# all of them: 80 tool calls x ~8k tokens = 640k. Summarization cannot rescue that,
# because it triggers at 170k and therefore has only 30k of headroom, while a
# single fan-out step was observed adding ~580k.
# Sized against that 30k headroom rather than a round number: at ~4 chars per
# token, 12000 chars is ~3k tokens, so an 8-way parallel fan-out adds ~24k and
# still fits. Only one output observed in production exceeded it (20,451 chars).
TOOL_OUTPUT_MAX_CHARS = int(os.getenv("TOOL_OUTPUT_MAX_CHARS", "12000"))

# Max characters of a single SkillHub skill's content that the read_skill tool
# returns to the model. Skills are meant to be short procedures, so a single
# skill should never approach this; it only caps a mis-sized one so a pasted-in
# long runbook can't bloat the context when the model reads it on demand.
SKILL_INJECT_MAX_CHARS = int(os.getenv("SKILL_INJECT_MAX_CHARS", "4000"))

# Anthropic prompt caching — caches the large static system prompt + tool
# definitions + growing message history so a multi-step loop is not re-billed
# full input tokens on every model call.
PROMPT_CACHING = os.getenv("PROMPT_CACHING", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Durable state
# ---------------------------------------------------------------------------
# Postgres DSN backing the langgraph checkpointer/store, the session table, the
# HITL audit log, and monitoring finding-state. When unset the process falls
# back to in-memory equivalents (fine for `python main.py` locally, but pending
# HITL approvals and monitoring state do NOT survive a restart).
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ---------------------------------------------------------------------------
# Monitoring
# ---------------------------------------------------------------------------
# Whether the background scheduler runs at all. This env var was previously
# documented and set in k8s/deployment.yaml but read by NO code, so the
# scheduler started unconditionally and MONITORING_ENABLED="false" was silently
# ignored. Defaults to true to preserve that de-facto behaviour when unset.
MONITORING_ENABLED = os.getenv("MONITORING_ENABLED", "true").lower() in ("1", "true", "yes")

# ---------------------------------------------------------------------------
# Monitoring notification policy
# ---------------------------------------------------------------------------
# The scheduled health check posts to Slack only when the diff against the
# previous run contains something new, escalated, or newly resolved. Without
# this a steady-state cluster problem is re-reported every interval forever,
# which is what made the scheduler too noisy to leave enabled.
MONITOR_NOTIFY_ON_RESOLVED = os.getenv("MONITOR_NOTIFY_ON_RESOLVED", "true").lower() in ("1", "true", "yes")

# Force a full report every N checks even when nothing changed, so a quiet
# channel still proves the bot is alive. 0 disables the digest entirely.
MONITOR_DIGEST_EVERY_N_CHECKS = int(os.getenv("MONITOR_DIGEST_EVERY_N_CHECKS", "12"))

# How long the Slack "Ack" button suppresses a finding from notifications.
MONITOR_ACK_HOURS = int(os.getenv("MONITOR_ACK_HOURS", "24"))

# ---------------------------------------------------------------------------
# HITL authorization
# ---------------------------------------------------------------------------
# Comma-separated Slack user IDs (e.g. "U123ABC,U456DEF") allowed to approve or
# reject cluster mutations. EMPTY MEANS ANY workspace user who can see the
# message may approve — which is the pre-existing behaviour, preserved as the
# default so enabling durability does not silently lock anyone out. Set this to
# lock approvals down to a named on-call group.
SLACK_APPROVER_IDS = {
    uid.strip() for uid in os.getenv("SLACK_APPROVER_IDS", "").split(",") if uid.strip()
}

# Unattended operation. When false, the change-executor's write tools stop
# raising an approval interrupt, so a run cannot stall waiting for a human that
# is not there.
#
# Two behaviours are deliberately tied to this one switch. With nobody to
# approve, the executor must also close its own loop: verify the change landed
# and, on failure, try a different approach instead of stopping at the first
# error. Leaving HITL on but retry off (or the reverse) describes a mode nobody
# runs, so it is one flag rather than two.
#
# Used by the ITBench harness at /root/WYB/itbench-eval. Leave true for
# interactive use:
# BOTH effects are what make it unsafe to enable casually, since writes then
# execute against whatever cluster the process is pointed at.
HITL_ENABLED = os.getenv("HITL_ENABLED", "true").lower() in ("1", "true", "yes")

# Attempts the executor may make at the same goal before reporting failure, in
# unattended mode only. Each attempt must be a *different* approach — repeating
# a fix that already failed is not a retry.
AUTONOMOUS_RETRY_LIMIT = int(os.getenv("AUTONOMOUS_RETRY_LIMIT", "3"))


def make_agent_config(thread_id: str) -> dict:
    """Build the langgraph invoke config for an agent run.

    Centralises the recursion_limit so every invoke / HITL resume across the
    API, Slack, scheduler, and CLI enforces the same hard loop cap.
    """
    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": RECURSION_LIMIT,
    }
