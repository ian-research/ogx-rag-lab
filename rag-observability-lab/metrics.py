"""A deliberately small Prometheus text exporter.

Jaeger holds traces; this holds the few counters a single-host lab needs for
E5 (requests, errors, latency distribution, model and tool outcomes). It is
not a general metrics pipeline: no external agent, no push, no retention.
"""
import threading

_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0)
_LOCK = threading.Lock()
_COUNTERS = {}
_HISTS = {}


def _escape(value):
    """Prometheus text format: escape backslash, double quote and newline."""
    return (str(value).replace("\\", "\\\\").replace("\n", "\\n")
            .replace('"', '\\"'))


def _labels(pairs):
    return ",".join('%s="%s"' % (k, _escape(v)) for k, v in sorted(pairs))


# One help/type line per metric name, as the exposition format requires.
_META = {
    "actionlab_http_requests_total":
        ("counter", "HTTP requests by matched route, method and status."),
    "actionlab_http_request_duration_seconds":
        ("histogram", "HTTP request duration by matched route and method."),
    "actionlab_model_calls_total":
        ("counter", "Model calls by outcome."),
    "actionlab_model_duration_seconds":
        ("histogram", "Model call duration by outcome."),
    "actionlab_model_prompt_tokens_total":
        ("counter", "Prompt tokens reported by the model."),
    "actionlab_model_completion_tokens_total":
        ("counter", "Completion tokens reported by the model."),
    "actionlab_tool_calls_total":
        ("counter", "Tool calls by tool and outcome (business outcomes included)."),
    "actionlab_tool_duration_seconds":
        ("histogram", "Tool call duration by tool."),
    "actionlab_submissions_total":
        ("counter", "Submission attempts by final state."),
}


def _inc(name, labels, value=1):
    key = (name, _labels(labels))
    with _LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0) + value


def _observe(name, labels, seconds):
    key = (name, _labels(labels))
    with _LOCK:
        entry = _HISTS.setdefault(key, {"counts": [0] * len(_BUCKETS),
                                        "sum": 0.0, "count": 0})
        for index, edge in enumerate(_BUCKETS):
            if seconds <= edge:
                entry["counts"][index] += 1
        entry["sum"] += seconds
        entry["count"] += 1


def observe_request(route, method, status, seconds):
    _inc("actionlab_http_requests_total",
         [("route", route), ("method", method), ("status", status)])
    _observe("actionlab_http_request_duration_seconds",
             [("route", route), ("method", method)], seconds)


def observe_model(result, seconds, prompt_tokens=None, completion_tokens=None):
    _inc("actionlab_model_calls_total", [("result", result)])
    _observe("actionlab_model_duration_seconds", [("result", result)], seconds)
    if prompt_tokens is not None:
        _inc("actionlab_model_prompt_tokens_total", [], prompt_tokens)
    if completion_tokens is not None:
        _inc("actionlab_model_completion_tokens_total", [], completion_tokens)


def observe_tool(tool, result, seconds):
    _inc("actionlab_tool_calls_total", [("tool", tool), ("result", result)])
    _observe("actionlab_tool_duration_seconds", [("tool", tool)], seconds)


def observe_submission(result):
    _inc("actionlab_submissions_total", [("result", result)])


def render():
    lines = []
    with _LOCK:
        counters = dict(_COUNTERS)
        hists = {key: {"counts": list(value["counts"]), "sum": value["sum"],
                       "count": value["count"]} for key, value in _HISTS.items()}
    seen = set()
    for (name, labels), value in sorted(counters.items()):
        if name not in seen:
            kind, help_text = _META.get(name, ("counter", ""))
            lines.append("# HELP %s %s" % (name, help_text))
            lines.append("# TYPE %s %s" % (name, kind))
            seen.add(name)
        suffix = "{%s}" % labels if labels else ""
        lines.append("%s%s %s" % (name, suffix, value))
    for (name, labels), value in sorted(hists.items()):
        if name not in seen:
            kind, help_text = _META.get(name, ("histogram", ""))
            lines.append("# HELP %s %s" % (name, help_text))
            lines.append("# TYPE %s %s" % (name, kind))
            seen.add(name)
        for edge, count in zip(_BUCKETS, value["counts"]):
            merged = labels + ("," if labels else "") + 'le="%s"' % edge
            lines.append("%s_bucket{%s} %d" % (name, merged, count))
        inf = labels + ("," if labels else "") + 'le="+Inf"'
        lines.append("%s_bucket{%s} %d" % (name, inf, value["count"]))
        suffix = "{%s}" % labels if labels else ""
        lines.append("%s_sum%s %.6f" % (name, suffix, value["sum"]))
        lines.append("%s_count%s %d" % (name, suffix, value["count"]))
    return "\n".join(lines) + "\n"


def reset():
    """Tests only."""
    with _LOCK:
        _COUNTERS.clear()
        _HISTS.clear()
