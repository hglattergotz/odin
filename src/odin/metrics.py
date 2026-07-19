"""Central, append-only run/task metrics.

Every `odin run` writes one JSONL file shared across all projects, so a single
machine accumulates a complete picture of what Odin does regardless of which
repo it was invoked from. Two record types share a `run_id`:

  - "task"  — one per task execution (completed / held / failed) with timing,
              token usage, cost, turn count, and outcome.
  - "run"   — one summary per `odin run` invocation (counts, totals, exit).

Design constraints (see CLAUDE.md):
  - Stdlib only. JSONL so `jq`/`duckdb`/pandas read it natively and a torn
    trailing line (crash mid-write) is trivially skippable.
  - Best-effort: a metrics failure must NEVER break a run. Every write is
    wrapped and swallowed.
  - Metadata only — never task bodies or agent output (they can carry secrets).
  - Concurrency-safe across processes: appends take an advisory `flock` so two
    Odin processes (e.g. one per project) can't tear each other's lines.

Location: $ODIN_HOME/metrics/events.jsonl (default ~/.odin/metrics/events.jsonl),
or $ODIN_METRICS_FILE to point at an explicit file. Disable with
$ODIN_NO_METRICS=1 (or `odin run --no-metrics`).
"""

from __future__ import annotations

import html
import json
import os
import socket
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median

try:
    import fcntl  # POSIX only; absent on Windows.
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - non-POSIX
    _HAVE_FCNTL = False

SCHEMA_VERSION = 1

_TOKEN_KEYS = ("input", "output", "cache_creation", "cache_read")

# Default Claude CLI usage-field names → Odin's internal token keys. Used when
# the usage dict is still raw (ClaudeBackend passes it through) and no
# `[platforms.<p>.metrics]` config override is present.
_DEFAULT_USAGE_RAW = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_creation": "cache_creation_input_tokens",
    "cache_read": "cache_read_input_tokens",
}

# Config keys under `[platforms.<p>.metrics]` that rename CLI fields → internal.
_METRICS_USAGE_CFG = {
    "input": "usage_input",
    "output": "usage_output",
    "cache_creation": "usage_cache_write",
    "cache_read": "usage_cache_read",
}


# ----------------------------------------------------------------------
# location / config
# ----------------------------------------------------------------------

def home_dir() -> Path:
    env = os.environ.get("ODIN_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".odin"


def events_path() -> Path:
    override = os.environ.get("ODIN_METRICS_FILE")
    if override:
        return Path(override).expanduser()
    return home_dir() / "metrics" / "events.jsonl"


def enabled() -> bool:
    """Metrics are on by default; $ODIN_NO_METRICS turns them off."""
    val = os.environ.get("ODIN_NO_METRICS", "").strip().lower()
    return val not in ("1", "true", "yes", "on")


def new_run_id() -> str:
    return uuid.uuid4().hex


# ----------------------------------------------------------------------
# writing
# ----------------------------------------------------------------------

def write_event(record: dict, *, path: Path | None = None) -> None:
    """Append one record as a single JSON line. Best-effort — never raises."""
    p = path or events_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(p, "a", encoding="utf-8") as f:
            if _HAVE_FCNTL:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
            try:
                f.write(line)
                f.flush()
            finally:
                if _HAVE_FCNTL:
                    try:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass
    except Exception:
        # Telemetry must never sink a run.
        pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _usage_raw_map(platform: str | None = None) -> dict[str, str]:
    """Map Odin internal token keys → raw CLI field names for `platform`.

    Reads `[platforms.<platform>.metrics]` when present; falls back to the
    Claude defaults. Best-effort — a bad config never raises.
    """
    mapping = dict(_DEFAULT_USAGE_RAW)
    if not platform:
        return mapping
    try:
        from odin import config as odin_config
        metrics_cfg = odin_config.get_in(
            odin_config.load_config(), f"platforms.{platform}.metrics"
        )
        if not isinstance(metrics_cfg, dict):
            return mapping
        for internal, cfg_key in _METRICS_USAGE_CFG.items():
            raw = metrics_cfg.get(cfg_key)
            if isinstance(raw, str) and raw.strip():
                mapping[internal] = raw.strip()
    except Exception:
        pass
    return mapping


def _norm_usage(usage: object, *, platform: str | None = None) -> dict:
    """Normalise a usage block to Odin's stable token keys.

    Accepts either:
      - a backend-normalised dict already keyed by ``input`` / ``output`` /
        ``cache_read`` / ``cache_creation`` (CursorBackend; pass-through), or
      - a raw CLI usage block, mapped via ``[platforms.<p>.metrics]`` config
        (or the Claude defaults when config is absent).
    """
    u = usage if isinstance(usage, dict) else {}
    # Already normalised by a backend? Prefer those keys so cursor token
    # counts aren't wiped by a Claude-shaped remap.
    if any(k in u for k in _TOKEN_KEYS):
        return {k: u.get(k) for k in _TOKEN_KEYS}
    raw_map = _usage_raw_map(platform)
    return {k: u.get(raw_map[k]) for k in _TOKEN_KEYS}


def agent_duration_ms(record: dict) -> object:
    """Read agent wall duration; accepts new and legacy JSONL field names."""
    if "agent_duration_ms" in record:
        return record["agent_duration_ms"]
    return record.get("claude_duration_ms")


def agent_api_ms(record: dict) -> object:
    """Read agent API duration; accepts new and legacy JSONL field names."""
    if "agent_api_ms" in record:
        return record["agent_api_ms"]
    return record.get("claude_api_ms")


# Coarse run-outcome label derived from Odin's exit code (see cli.py).
_STOP = {
    0: "drained_or_capped",
    1: "failed",
    2: "setup_error",
    10: "held",
    11: "urgent_halt",
}


def _stop_for(exit_code: int) -> str:
    return _STOP.get(exit_code, f"exit_{exit_code}")


class RunAccumulator:
    """Collects per-task records during one `odin run` and emits a run summary.

    Created after branch resolution, fed one `record_task` per task execution,
    and `finish(exit_code)` is called from a `finally` so the run summary is
    written on every exit path (drain, fail, held, halt). When `enabled` is
    False (metrics off, or --dry-run) it tracks nothing and writes nothing.
    """

    def __init__(
        self,
        *,
        run_id: str,
        project: object,
        queue: object,
        branch: str | None,
        platform: str | None = None,
        enabled: bool = True,
        path: Path | None = None,
    ) -> None:
        self.run_id = run_id
        self.project = str(project)
        self.queue = str(queue)
        self.branch = branch
        self.platform = platform
        self.enabled = enabled
        self.path = path
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self._start = _now()
        self._mono = time.monotonic()
        self.completed = 0
        self.failed = 0
        self.held = 0
        self.cost_total = 0.0
        self._any_cost = False  # True once any task yields a numeric cost_usd
        self.tokens_total = {k: 0 for k in _TOKEN_KEYS}
        self._finished = False

    def record_task(self, *, task_stem: str, outcome: str, result: object) -> None:
        if outcome == "completed":
            self.completed += 1
        elif outcome == "held":
            self.held += 1
        else:
            self.failed += 1

        platform = getattr(result, "platform", None) or self.platform
        usage = _norm_usage(getattr(result, "usage", None), platform=platform)
        cost = getattr(result, "cost_usd", None)
        if isinstance(cost, (int, float)):
            self.cost_total += cost
            self._any_cost = True
        for k in _TOKEN_KEYS:
            v = usage.get(k)
            if isinstance(v, (int, float)):
                self.tokens_total[k] += v

        if not self.enabled:
            return
        write_event(
            {
                "type": "task",
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "ts": _now().isoformat(),  # task end (start ≈ ts - wall_ms)
                "host": self.host,
                "pid": self.pid,
                "project": self.project,
                "queue": self.queue,
                "branch": self.branch,
                "platform": platform,
                "task": task_stem,
                "outcome": outcome,
                "stop_reason": getattr(result, "stop_reason", None),
                "error": getattr(result, "error", None),
                "wall_ms": getattr(result, "wall_ms", None),
                "agent_duration_ms": getattr(result, "duration_ms", None),
                "agent_api_ms": getattr(result, "api_ms", None),
                "num_turns": getattr(result, "num_turns", None),
                "tokens": usage,
                "cost_usd": cost,
                "session_id": getattr(result, "session_id", None),
            },
            path=self.path,
        )

    def finish(self, exit_code: int) -> None:
        if self._finished:
            return
        self._finished = True
        if not self.enabled:
            return
        tasks_total = self.completed + self.failed + self.held
        if tasks_total == 0:
            # Nothing ran (empty queue, setup error) — don't log a hollow run.
            return
        write_event(
            {
                "type": "run",
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "ts_start": self._start.isoformat(),
                "ts_end": _now().isoformat(),
                "host": self.host,
                "pid": self.pid,
                "project": self.project,
                "queue": self.queue,
                "branch": self.branch,
                "platform": self.platform,
                "tasks_completed": self.completed,
                "tasks_failed": self.failed,
                "tasks_held": self.held,
                "tasks_total": tasks_total,
                "wall_ms": int((time.monotonic() - self._mono) * 1000),
                "tokens_total": dict(self.tokens_total),
                # null when no task reported a numeric cost (Cursor has none);
                # 0.0 only when costs were seen and summed to zero.
                "cost_usd_total": (
                    round(self.cost_total, 6) if self._any_cost else None
                ),
                "exit_code": exit_code,
                "stop": _stop_for(exit_code),
            },
            path=self.path,
        )


# ----------------------------------------------------------------------
# reading / aggregation
# ----------------------------------------------------------------------

def read_events(path: Path | None = None) -> list[dict]:
    """Read all JSONL records, skipping blank/corrupt lines."""
    p = path or events_path()
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _parse_ts(s: object) -> datetime | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _stats(values: list) -> dict:
    vals = [v for v in values if isinstance(v, (int, float))]
    if not vals:
        return {"n": 0, "total": 0, "mean": None, "median": None, "max": None}
    return {
        "n": len(vals),
        "total": sum(vals),
        "mean": mean(vals),
        "median": median(vals),
        "max": max(vals),
    }


def _peak_concurrency(runs: list[dict]) -> tuple[int, str | None]:
    """Max number of `odin run` invocations whose [start, end] intervals overlap."""
    points: list[tuple[datetime, int]] = []
    for r in runs:
        s = _parse_ts(r.get("ts_start"))
        e = _parse_ts(r.get("ts_end"))
        if s and e:
            points.append((s, 1))
            points.append((e, -1))
    # At an equal instant, opens (+1) sort before closes (-1) so touching
    # intervals count as concurrent.
    points.sort(key=lambda x: (x[0], -x[1]))
    cur = peak = 0
    peak_at: datetime | None = None
    for t, delta in points:
        cur += delta
        if cur > peak:
            peak, peak_at = cur, t
    return peak, peak_at.isoformat() if peak_at else None


def _empty_tokens() -> dict:
    return {k: 0 for k in _TOKEN_KEYS}


def aggregate(
    events: list[dict],
    *,
    project_filter: str | None = None,
    path: Path | None = None,
) -> dict:
    """Roll raw events up into the structure both renderers consume."""
    tasks = [e for e in events if e.get("type") == "task"]
    runs = [e for e in events if e.get("type") == "run"]
    if project_filter:
        tasks = [t for t in tasks if project_filter in str(t.get("project", ""))]
        runs = [r for r in runs if project_filter in str(r.get("project", ""))]

    outcomes = {"completed": 0, "held": 0, "failed": 0}
    tokens_total = _empty_tokens()
    cost_total = 0.0
    any_cost = False
    for t in tasks:
        outcomes[t.get("outcome", "failed")] = outcomes.get(t.get("outcome", "failed"), 0) + 1
        tk = t.get("tokens") or {}
        for k in _TOKEN_KEYS:
            v = tk.get(k)
            if isinstance(v, (int, float)):
                tokens_total[k] += v
        c = t.get("cost_usd")
        if isinstance(c, (int, float)):
            cost_total += c
            any_cost = True

    # Per-project rollup.
    by_project: dict[str, dict] = {}
    for t in tasks:
        proj = str(t.get("project", "?"))
        p = by_project.setdefault(
            proj,
            {
                "project": proj,
                "tasks": 0, "completed": 0, "held": 0, "failed": 0,
                "runs": 0, "cost": 0.0, "_any_cost": False,
                "tokens": _empty_tokens(),
                "_wall": [],
            },
        )
        p["tasks"] += 1
        p[t.get("outcome", "failed")] = p.get(t.get("outcome", "failed"), 0) + 1
        c = t.get("cost_usd")
        if isinstance(c, (int, float)):
            p["cost"] += c
            p["_any_cost"] = True
        tk = t.get("tokens") or {}
        for k in _TOKEN_KEYS:
            v = tk.get(k)
            if isinstance(v, (int, float)):
                p["tokens"][k] += v
        p["_wall"].append(t.get("wall_ms"))
    run_count_by_project: dict[str, int] = {}
    for r in runs:
        proj = str(r.get("project", "?"))
        run_count_by_project[proj] = run_count_by_project.get(proj, 0) + 1
    projects = []
    for proj, p in by_project.items():
        st = _stats(p.pop("_wall"))
        any_p = p.pop("_any_cost")
        p["runs"] = run_count_by_project.get(proj, 0)
        p["cost"] = p["cost"] if any_p else None
        p["task_mean_ms"] = st["mean"]
        p["task_median_ms"] = st["median"]
        projects.append(p)
    projects.sort(key=lambda p: p["tasks"], reverse=True)

    # Per-platform rollup (only surfaced by renderers when mixed).
    by_platform: dict[str, dict] = {}
    for t in tasks:
        plat = str(t.get("platform") or "?")
        pl = by_platform.setdefault(
            plat,
            {
                "platform": plat,
                "tasks": 0, "completed": 0, "held": 0, "failed": 0,
                "cost": 0.0, "_any_cost": False,
                "tokens": _empty_tokens(),
            },
        )
        pl["tasks"] += 1
        pl[t.get("outcome", "failed")] = pl.get(t.get("outcome", "failed"), 0) + 1
        c = t.get("cost_usd")
        if isinstance(c, (int, float)):
            pl["cost"] += c
            pl["_any_cost"] = True
        tk = t.get("tokens") or {}
        for k in _TOKEN_KEYS:
            v = tk.get(k)
            if isinstance(v, (int, float)):
                pl["tokens"][k] += v
    platforms = []
    for plat, pl in by_platform.items():
        any_pl = pl.pop("_any_cost")
        pl["cost"] = pl["cost"] if any_pl else None
        platforms.append(pl)
    platforms.sort(key=lambda p: p["tasks"], reverse=True)

    peak, peak_at = _peak_concurrency(runs)

    recent = sorted(runs, key=lambda r: str(r.get("ts_end", "")), reverse=True)[:20]
    recent_runs = [
        {
            "ts_end": r.get("ts_end"),
            "project": r.get("project"),
            "branch": r.get("branch"),
            "platform": r.get("platform"),
            "tasks_total": r.get("tasks_total"),
            "completed": r.get("tasks_completed"),
            "failed": r.get("tasks_failed"),
            "held": r.get("tasks_held"),
            "wall_ms": r.get("wall_ms"),
            "cost": r.get("cost_usd_total"),
            "stop": r.get("stop"),
        }
        for r in recent
    ]

    return {
        "events_path": str(path or events_path()),
        "project_filter": project_filter,
        "n_runs": len(runs),
        "n_tasks": len(tasks),
        "outcomes": outcomes,
        "cost_total": cost_total if any_cost else None,
        "tokens_total": tokens_total,
        "task_wall": _stats([t.get("wall_ms") for t in tasks]),
        "run_wall": _stats([r.get("wall_ms") for r in runs]),
        "peak_concurrency": peak,
        "peak_at": peak_at,
        "projects": projects,
        "platforms": platforms,
        "recent_runs": recent_runs,
    }


# ----------------------------------------------------------------------
# formatting helpers
# ----------------------------------------------------------------------

def _fmt_ms(ms: object) -> str:
    if not isinstance(ms, (int, float)):
        return "-"
    s = ms / 1000.0
    if s < 60:
        return f"{s:.1f}s"
    m, sec = divmod(int(s), 60)
    if m < 60:
        return f"{m}m {sec}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _fmt_cost(c: object) -> str:
    if c is None:
        return "-"
    return f"${c:,.2f}" if isinstance(c, (int, float)) else "-"


def _sum_costs(costs: list) -> float | None:
    """Sum numeric costs; ``None`` when no numeric cost was present."""
    nums = [c for c in costs if isinstance(c, (int, float))]
    return sum(nums) if nums else None


def _fmt_int(n: object) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) else "0"


def _short_proj(proj: str) -> str:
    """Trailing path component(s) — full paths are long and repetitive."""
    parts = Path(proj).parts
    return "/".join(parts[-2:]) if len(parts) >= 2 else proj


def _tokens_line(tokens: dict) -> str:
    return (
        f"in {_fmt_int(tokens.get('input'))}, "
        f"out {_fmt_int(tokens.get('output'))}, "
        f"cache-read {_fmt_int(tokens.get('cache_read'))}, "
        f"cache-write {_fmt_int(tokens.get('cache_creation'))}"
    )


# ----------------------------------------------------------------------
# text report
# ----------------------------------------------------------------------

def render_text(agg: dict) -> str:
    lines: list[str] = []
    lines.append(f"Odin metrics — {agg['events_path']}")
    if agg.get("project_filter"):
        lines.append(f"(filtered to project containing: {agg['project_filter']})")
    if agg["n_tasks"] == 0 and agg["n_runs"] == 0:
        lines.append("")
        lines.append("No metrics recorded yet. Run `odin run` to start collecting.")
        return "\n".join(lines) + "\n"

    tw, rw = agg["task_wall"], agg["run_wall"]
    peak_at = agg["peak_at"]
    lines += [
        "",
        "Overall",
        f"  runs: {agg['n_runs']}    tasks: {agg['n_tasks']}    "
        f"peak concurrent runs: {agg['peak_concurrency']}"
        + (f" (at {peak_at})" if peak_at else ""),
        f"  outcomes: {agg['outcomes']['completed']} completed, "
        f"{agg['outcomes']['held']} held, {agg['outcomes']['failed']} failed",
        f"  cost: {_fmt_cost(agg['cost_total'])}    "
        f"tokens: {_tokens_line(agg['tokens_total'])}",
        f"  task time: mean {_fmt_ms(tw['mean'])}, median {_fmt_ms(tw['median'])}, "
        f"max {_fmt_ms(tw['max'])}, total {_fmt_ms(tw['total'])}",
        f"  run time:  mean {_fmt_ms(rw['mean'])}, median {_fmt_ms(rw['median'])}, "
        f"max {_fmt_ms(rw['max'])}",
    ]

    if agg["projects"]:
        lines += ["", "By project"]
        name_w = max(len(_short_proj(p["project"])) for p in agg["projects"])
        name_w = min(max(name_w, 7), 40)
        header = (
            f"  {'project'.ljust(name_w)}  {'runs':>4}  {'tasks':>5}  "
            f"{'done':>4}  {'held':>4}  {'fail':>4}  {'cost':>9}  {'avg-task':>9}"
        )
        lines.append(header)
        for p in agg["projects"]:
            lines.append(
                f"  {_short_proj(p['project']).ljust(name_w)[:name_w]}  "
                f"{p['runs']:>4}  {p['tasks']:>5}  {p['completed']:>4}  "
                f"{p['held']:>4}  {p['failed']:>4}  "
                f"{_fmt_cost(p['cost']):>9}  {_fmt_ms(p['task_mean_ms']):>9}"
            )
        # Total row across all projects; avg-task uses the global task mean.
        ps = agg["projects"]
        lines.append("  " + "-" * (len(header) - 2))
        lines.append(
            f"  {'TOTAL'.ljust(name_w)[:name_w]}  "
            f"{sum(p['runs'] for p in ps):>4}  {sum(p['tasks'] for p in ps):>5}  "
            f"{sum(p['completed'] for p in ps):>4}  {sum(p['held'] for p in ps):>4}  "
            f"{sum(p['failed'] for p in ps):>4}  "
            f"{_fmt_cost(_sum_costs([p['cost'] for p in ps])):>9}  "
            f"{_fmt_ms(agg['task_wall']['mean']):>9}"
        )

    # Platform breakdown only when more than one platform appears in the data.
    platforms = agg.get("platforms") or []
    if len(platforms) > 1:
        lines += ["", "By platform"]
        name_w = max(len(str(p["platform"])) for p in platforms)
        name_w = min(max(name_w, 8), 20)
        header = (
            f"  {'platform'.ljust(name_w)}  {'tasks':>5}  "
            f"{'done':>4}  {'held':>4}  {'fail':>4}  {'cost':>9}"
        )
        lines.append(header)
        for p in platforms:
            lines.append(
                f"  {str(p['platform']).ljust(name_w)[:name_w]}  "
                f"{p['tasks']:>5}  {p['completed']:>4}  "
                f"{p['held']:>4}  {p['failed']:>4}  "
                f"{_fmt_cost(p['cost']):>9}"
            )

    if agg["recent_runs"]:
        lines += ["", "Recent runs"]
        lines.append(
            f"  {'finished':<19}  {'project':<24}  {'done/total':>10}  "
            f"{'cost':>9}  {'wall':>9}  stop"
        )
        for r in agg["recent_runs"]:
            ts = (r["ts_end"] or "")[:19].replace("T", " ")
            done = f"{r['completed']}/{r['tasks_total']}"
            lines.append(
                f"  {ts:<19}  {_short_proj(str(r['project'])):<24.24}  {done:>10}  "
                f"{_fmt_cost(r['cost']):>9}  {_fmt_ms(r['wall_ms']):>9}  {r['stop']}"
            )

    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# html report
# ----------------------------------------------------------------------

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, system-ui, Segoe UI, Roboto, sans-serif;
  margin: 0; padding: 2rem; background: #0f1115; color: #e6e6e6; }
h1 { font-size: 1.4rem; margin: 0 0 .25rem; }
h2 { font-size: 1.05rem; margin: 2rem 0 .75rem; color: #9fb3c8; }
.sub { color: #7a8aa0; font-size: .85rem; margin-bottom: 1.5rem; }
.cards { display: grid; gap: 1rem; grid-template-columns: repeat(auto-fit,minmax(160px,1fr)); }
.card { background: #1a1e26; border: 1px solid #2a3038; border-radius: 10px; padding: 1rem; }
.card .v { font-size: 1.6rem; font-weight: 600; }
.card .l { color: #7a8aa0; font-size: .8rem; text-transform: uppercase; letter-spacing: .04em; }
table { width: 100%; border-collapse: collapse; margin-top: .5rem; font-variant-numeric: tabular-nums; }
th, td { text-align: right; padding: .45rem .6rem; border-bottom: 1px solid #232833; }
th:first-child, td:first-child { text-align: left; }
th { color: #9fb3c8; font-weight: 600; font-size: .8rem; text-transform: uppercase; letter-spacing: .03em; }
tr:hover td { background: #161a21; }
tfoot td { border-top: 2px solid #3a4350; font-weight: 600; }
.bar { background: #2a3a55; height: 8px; border-radius: 4px; min-width: 2px; }
.bar-cell { width: 120px; }
.muted { color: #7a8aa0; }
.ok { color: #4ec9a0; } .warn { color: #d7a44a; } .bad { color: #e06c6c; }
code { background: #1a1e26; padding: .1rem .35rem; border-radius: 4px; }
"""


def _card(value: str, label: str) -> str:
    return f'<div class="card"><div class="v">{html.escape(value)}</div>' \
           f'<div class="l">{html.escape(label)}</div></div>'


def render_html(agg: dict) -> str:
    e = html.escape
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    head = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Odin metrics</title><style>{_CSS}</style></head><body>"
    )
    parts = [head, "<h1>Odin metrics</h1>"]
    filt = f" · filtered to <code>{e(agg['project_filter'])}</code>" if agg.get("project_filter") else ""
    parts.append(
        f'<div class="sub">{e(agg["events_path"])} · generated {e(generated)}{filt}</div>'
    )

    if agg["n_tasks"] == 0 and agg["n_runs"] == 0:
        parts.append('<p class="muted">No metrics recorded yet. '
                     "Run <code>odin run</code> to start collecting.</p></body></html>")
        return "".join(parts)

    tw, rw = agg["task_wall"], agg["run_wall"]
    tok = agg["tokens_total"]
    total_tokens = sum(v for v in tok.values() if isinstance(v, (int, float)))
    parts.append('<div class="cards">')
    parts += [
        _card(str(agg["n_runs"]), "runs"),
        _card(str(agg["n_tasks"]), "tasks"),
        _card(str(agg["peak_concurrency"]), "peak concurrent runs"),
        _card(_fmt_cost(agg["cost_total"]), "total cost"),
        _card(_fmt_int(total_tokens), "total tokens"),
        _card(_fmt_ms(tw["mean"]), "avg task time"),
        _card(_fmt_ms(tw["median"]), "median task time"),
        _card(_fmt_ms(rw["mean"]), "avg run time"),
    ]
    parts.append("</div>")

    o = agg["outcomes"]
    parts.append("<h2>Outcomes</h2><div class=cards>")
    parts += [
        f'<div class="card"><div class="v ok">{o["completed"]}</div><div class=l>completed</div></div>',
        f'<div class="card"><div class="v warn">{o["held"]}</div><div class=l>held</div></div>',
        f'<div class="card"><div class="v bad">{o["failed"]}</div><div class=l>failed</div></div>',
    ]
    parts.append("</div>")

    if agg["projects"]:
        max_tasks = max(p["tasks"] for p in agg["projects"]) or 1
        parts.append("<h2>By project</h2><table><thead><tr>"
                     "<th>project</th><th></th><th>runs</th><th>tasks</th>"
                     "<th>done</th><th>held</th><th>fail</th>"
                     "<th>cost</th><th>tokens</th><th>avg task</th>"
                     "</tr></thead><tbody>")
        for p in agg["projects"]:
            pct = int(100 * p["tasks"] / max_tasks)
            ptok = sum(v for v in p["tokens"].values() if isinstance(v, (int, float)))
            parts.append(
                "<tr>"
                f"<td title='{e(p['project'])}'>{e(_short_proj(p['project']))}</td>"
                f"<td class=bar-cell><div class=bar style='width:{pct}%'></div></td>"
                f"<td>{p['runs']}</td><td>{p['tasks']}</td>"
                f"<td class=ok>{p['completed']}</td>"
                f"<td class=warn>{p['held']}</td>"
                f"<td class=bad>{p['failed']}</td>"
                f"<td>{e(_fmt_cost(p['cost']))}</td>"
                f"<td>{e(_fmt_int(ptok))}</td>"
                f"<td>{e(_fmt_ms(p['task_mean_ms']))}</td>"
                "</tr>"
            )
        ps = agg["projects"]
        t_tok = sum(sum(v for v in p["tokens"].values() if isinstance(v, (int, float)))
                    for p in ps)
        parts.append(
            "</tbody><tfoot><tr>"
            "<td>TOTAL</td><td></td>"
            f"<td>{sum(p['runs'] for p in ps)}</td>"
            f"<td>{sum(p['tasks'] for p in ps)}</td>"
            f"<td class=ok>{sum(p['completed'] for p in ps)}</td>"
            f"<td class=warn>{sum(p['held'] for p in ps)}</td>"
            f"<td class=bad>{sum(p['failed'] for p in ps)}</td>"
            f"<td>{e(_fmt_cost(_sum_costs([p['cost'] for p in ps])))}</td>"
            f"<td>{e(_fmt_int(t_tok))}</td>"
            f"<td>{e(_fmt_ms(agg['task_wall']['mean']))}</td>"
            "</tr></tfoot></table>"
        )

    platforms = agg.get("platforms") or []
    if len(platforms) > 1:
        parts.append("<h2>By platform</h2><table><thead><tr>"
                     "<th>platform</th><th>tasks</th>"
                     "<th>done</th><th>held</th><th>fail</th>"
                     "<th>cost</th><th>tokens</th>"
                     "</tr></thead><tbody>")
        for p in platforms:
            ptok = sum(v for v in p["tokens"].values() if isinstance(v, (int, float)))
            parts.append(
                "<tr>"
                f"<td>{e(str(p['platform']))}</td>"
                f"<td>{p['tasks']}</td>"
                f"<td class=ok>{p['completed']}</td>"
                f"<td class=warn>{p['held']}</td>"
                f"<td class=bad>{p['failed']}</td>"
                f"<td>{e(_fmt_cost(p['cost']))}</td>"
                f"<td>{e(_fmt_int(ptok))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")

    if agg["recent_runs"]:
        parts.append("<h2>Recent runs</h2><table><thead><tr>"
                     "<th>finished</th><th>project</th><th>branch</th>"
                     "<th>done/total</th><th>fail</th><th>held</th>"
                     "<th>cost</th><th>wall</th><th>stop</th>"
                     "</tr></thead><tbody>")
        for r in agg["recent_runs"]:
            ts = e((r["ts_end"] or "")[:19].replace("T", " "))
            parts.append(
                "<tr>"
                f"<td>{ts}</td>"
                f"<td title='{e(str(r['project']))}'>{e(_short_proj(str(r['project'])))}</td>"
                f"<td>{e(str(r['branch'] or '-'))}</td>"
                f"<td>{r['completed']}/{r['tasks_total']}</td>"
                f"<td class=bad>{r['failed']}</td>"
                f"<td class=warn>{r['held']}</td>"
                f"<td>{e(_fmt_cost(r['cost']))}</td>"
                f"<td>{e(_fmt_ms(r['wall_ms']))}</td>"
                f"<td>{e(str(r['stop']))}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")

    parts.append("</body></html>")
    return "".join(parts)
