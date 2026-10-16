"""Agent-facing Prometheus query tools.

These expose PromQL to a language model, which is a different risk profile from
the Kubernetes read tools: a kubectl call costs one API round-trip and returns
one object, while a PromQL expression can ask Prometheus to scan its entire
time series database on behalf of every dashboard that shares it.

So the cost policy lives here in code, not in the tool signatures — same reason
`search_knowledge` fixes its own min_score rather than letting the model pass
one: an argument a model can set is an argument a model can defeat. Concretely:

  * `__name__` matched with a regex is refused. That is the idiom for "scan
    everything", and no legitimate diagnostic question here needs it.
  * The range window is clamped, whatever the caller asks for.
  * At most PROMETHEUS_MAX_SERIES series and PROMETHEUS_MAX_POINTS per series
    reach the transcript, with an explicit marker naming what was withheld —
    a truncated result the model knows is truncated can be narrowed; a silently
    truncated one gets read as complete.
  * The HTTP client applies an 8s timeout, which bounds the server-side work a
    pathologically expensive expression can do before it is abandoned.
"""
from __future__ import annotations

import re

from langchain.tools import tool

from config import (
    PROMETHEUS_MAX_POINTS,
    PROMETHEUS_MAX_RANGE_MINUTES,
    PROMETHEUS_MAX_SERIES,
)
from tools.prometheus import (
    PrometheusUnavailable,
    metric_names,
    query,
    query_range,
)

# Range queries are downsampled to this many points per series. Enough to see
# the shape of a trend, small enough that a dozen series still fit in a prompt.
_RANGE_POINTS = 12

# Trends are usually read per workload; fewer series than the instant-query cap
# keeps a range result from crowding out the rest of the evidence.
_RANGE_MAX_SERIES = min(PROMETHEUS_MAX_SERIES, 10)

# `__name__` with any non-exact matcher (`=~` / `!~` / `!=`) is the standard way
# to address every series in the database.
_FULL_SCAN = re.compile(r"__name__\s*(=~|!~|!=)")


def _unavailable(exc: Exception, action: str) -> str:
    return (
        f"ERROR: Prometheus is unavailable, so {action} could not run ({exc}). "
        "Fall back to kubectl evidence, and do not assume a value of zero — "
        "an unreachable metrics backend is not an idle cluster."
    )


def _fmt(value: float) -> str:
    """Compact numeric formatting: 4 significant digits, no trailing zeros."""
    if value != value:  # NaN
        return "NaN"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.4g}"


def _labels(metric: dict) -> str:
    """Render a series' identity, dropping the boilerplate Prometheus adds.

    instance/job/pod are kept; __name__ is redundant (the query named it) and
    endpoint/container are noise on a single-node cluster.
    """
    skip = {"__name__", "endpoint", "container", "service", "namespace"}
    parts = [
        f"{k}={v}"
        for k, v in sorted(metric.items())
        if k not in skip and v not in ("", None)
    ]
    if not parts:
        return "(no labels)"
    return "{" + ", ".join(parts) + "}"


@tool
def prometheus_query(promql: str) -> str:
    """Run an instant PromQL query and return current values.

    Use this for "what is it right now" questions: current CPU or memory usage,
    whether a target is up, how many containers are running. Returns the current
    sample only — for how a value has changed over time, use
    prometheus_query_range instead.

    If you do not know which metric name to use, call prometheus_list_metrics
    first rather than guessing; a plausible-but-absent name returns nothing and
    wastes a call.

    Results are capped at the top series by value. The output states the total
    when it withholds any, so narrow the selector with a label matcher (for
    example {namespace="prod"}) rather than assuming you saw everything.
    """
    expression = (promql or "").strip()
    if not expression:
        return "ERROR: prometheus_query requires a non-empty PromQL expression."
    if _FULL_SCAN.search(expression):
        return (
            "ERROR: a regex __name__ matcher would scan every series in the "
            "database and is refused. Select a metric by name instead, for "
            "example container_memory_working_set_bytes{namespace=\"prod\"}."
        )

    try:
        rows = query(expression)
    except PrometheusUnavailable as exc:
        return _unavailable(exc, "the instant query")
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: PromQL parse or execution failure: {type(exc).__name__}: {exc}"

    if not rows:
        return (
            "NO DATA: the expression is valid but matched no series. Check the "
            "metric name with prometheus_list_metrics, or relax the label "
            "matchers. This is an empty result, not evidence that the value is 0."
        )

    rows.sort(key=lambda r: r[1], reverse=True)
    shown = rows[:PROMETHEUS_MAX_SERIES]
    lines = [f"PROMETHEUS INSTANT QUERY: {expression}", f"{len(rows)} series matched."]
    for metric, value in shown:
        lines.append(f"  {_fmt(value)}  {_labels(metric)}")
    if len(rows) > len(shown):
        lines.append(
            f"  [{len(rows) - len(shown)} further series withheld — the list is "
            "capped, not complete. Add a label matcher to narrow it.]"
        )
    return "\n".join(lines)


@tool
def prometheus_query_range(promql: str, minutes: int = 60) -> str:
    """Run a PromQL query over a time window and return the trend.

    Use this for change over time: is memory climbing, did CPU spike during the
    incident, is a gauge oscillating. Returns first/last/min/max/mean plus a
    downsampled series for each match.

    `minutes` is the lookback window. It is clamped to a maximum, so asking for
    a very wide window returns the clamped one rather than failing.

    Keep the expression aggregated — a bare selector matches one series per
    container and will be capped. Wrap it in an aggregation, for example
    sum by (pod) (rate(container_cpu_usage_seconds_total[5m])).
    """
    expression = (promql or "").strip()
    if not expression:
        return "ERROR: prometheus_query_range requires a non-empty PromQL expression."
    if _FULL_SCAN.search(expression):
        return (
            "ERROR: a regex __name__ matcher would scan every series in the "
            "database and is refused. Select a metric by name instead."
        )

    try:
        requested = int(minutes)
    except (TypeError, ValueError):
        return "ERROR: minutes must be an integer number of minutes."
    window = max(1, min(requested, PROMETHEUS_MAX_RANGE_MINUTES))
    clamped = "" if window == requested else f" (clamped from {requested})"

    # Chosen so the response never carries more than PROMETHEUS_MAX_POINTS
    # samples per series, whatever the window.
    step = max(15, (window * 60) // PROMETHEUS_MAX_POINTS)

    try:
        series = query_range(expression, window * 60, step)
    except PrometheusUnavailable as exc:
        return _unavailable(exc, "the range query")
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: PromQL parse or execution failure: {type(exc).__name__}: {exc}"

    if not series:
        return (
            "NO DATA: the expression is valid but matched no series in this "
            "window. This is an empty result, not evidence that the value is 0."
        )

    series.sort(key=lambda s: max(p[1] for p in s["points"]), reverse=True)
    shown = series[:_RANGE_MAX_SERIES]

    lines = [
        f"PROMETHEUS RANGE QUERY: {expression}",
        f"window={window}m{clamped}  step={step}s  {len(series)} series matched.",
    ]
    for entry in shown:
        points = entry["points"]
        values = [p[1] for p in points]
        peak = max(values)
        lines.append(
            f"  {_labels(entry['labels'])}  "
            f"first={_fmt(values[0])} last={_fmt(values[-1])} "
            f"min={_fmt(min(values))} max={_fmt(peak)} "
            f"mean={_fmt(sum(values) / len(values))}"
        )
        # Evenly spaced so the shape survives however many raw points there were.
        picks = sorted({
            round(i * (len(points) - 1) / (_RANGE_POINTS - 1))
            for i in range(_RANGE_POINTS)
        }) if len(points) > 1 else [0]
        end = points[-1][0]
        trend = ", ".join(
            f"-{int((end - points[i][0]) / 60)}m:{_fmt(points[i][1])}" for i in picks
        )
        lines.append(f"    [{trend}]")

    if len(series) > len(shown):
        lines.append(
            f"  [{len(series) - len(shown)} further series withheld — the list is "
            "capped, not complete. Aggregate or add a label matcher.]"
        )
    return "\n".join(lines)


@tool
def prometheus_list_metrics(pattern: str = "") -> str:
    """List Prometheus metric names available in this cluster.

    Call this before querying when you are unsure of a metric's exact name —
    metric names are verbose and easy to misremember
    (container_memory_working_set_bytes, not container_memory_usage).
    `pattern` is an optional case-insensitive substring filter, for example
    "cpu", "memory", "filesystem", or "kube_".
    """
    try:
        names = metric_names(pattern)
    except PrometheusUnavailable as exc:
        return _unavailable(exc, "the metric list")
    except Exception as exc:  # noqa: BLE001
        return f"ERROR: could not list metrics: {type(exc).__name__}: {exc}"

    if not names:
        return (
            f"NO MATCH: no metric name contains {pattern!r}. "
            "Call again without a pattern to see what exists."
        )

    header = f"PROMETHEUS METRIC NAMES matching {pattern!r}:" if pattern else "PROMETHEUS METRIC NAMES:"
    lines = [header, f"{len(names)} name(s):"]
    lines.extend(f"  {n}" for n in names)
    lines.append(
        "NOTE: names only. Use prometheus_query(metric) to see its current "
        "series, or prometheus_query_range(metric) for its trend."
    )
    return "\n".join(lines)
