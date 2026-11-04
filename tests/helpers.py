"""Shared helpers for the pytest suite. All data here is synthetic.

usage_verdict.py reads --dir / $USAGE_VERDICT_NOW / the config at import time,
so every case runs it as a subprocess against a throwaway state dir. The
subprocess TZ defaults to UTC so results never depend on the host timezone.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERDICT = os.path.join(ROOT, "usage_verdict.py")
GUARD = os.path.join(ROOT, "budget-guard.cjs")
HAS_NODE = shutil.which("node") is not None

# Fixed synthetic "now": 2026-11-04 10:00:00 UTC (arbitrary, not a real snapshot).
NOW = int(datetime(2026, 11, 4, 10, 0, 0, tzinfo=timezone.utc).timestamp())
MIN = 60
H = 3600
DAY = 86400


def five_hour(pct, remain_min, now=NOW):
    """five_hour block with `remain_min` minutes left in the 300-min window."""
    return {"used_percentage": pct, "resets_at": now + remain_min * 60}


def seven_day(pct, remain_hours, now=NOW):
    return {"used_percentage": pct, "resets_at": now + remain_hours * 3600}


def make_dir(limits=None, history=None, config=None, raw_limits=None, raw_history=None, raw_config=None):
    """Create a state dir. `limits` (dict) is JSON-dumped; `raw_*` write bytes/str verbatim."""
    d = tempfile.mkdtemp(prefix="pacer-pytest-")
    if raw_limits is not None:
        _write(os.path.join(d, "limits.json"), raw_limits)
    elif limits is not None:
        _write(os.path.join(d, "limits.json"), json.dumps(limits))
    if raw_history is not None:
        _write(os.path.join(d, "limits-history.jsonl"), raw_history)
    elif history is not None:
        _write(os.path.join(d, "limits-history.jsonl"), "".join(json.dumps(s) + "\n" for s in history))
    if raw_config is not None:
        _write(os.path.join(d, "config.json"), raw_config)
    elif config is not None:
        _write(os.path.join(d, "config.json"), json.dumps(config))
    return d


def _write(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)


def run_py(args, state_dir, now=NOW, tz="UTC", extra_env=None):
    env = dict(os.environ, TZ=tz)
    env.pop("CLAUDE_PACER_DIR", None)
    env.pop("USAGE_VERDICT_NOW", None)
    if now is not None:
        env["USAGE_VERDICT_NOW"] = str(now)
    if extra_env:
        env.update(extra_env)
    r = subprocess.run([sys.executable, VERDICT, "--dir", state_dir] + list(args),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
    return r


class Result:
    def __init__(self, text_run, json_run):
        self.code = text_run.returncode
        self.text = text_run.stdout
        self.stderr = text_run.stderr
        self.json_run = json_run
        self._json = None

    @property
    def j(self):
        return json.loads(self.json_run.stdout)


def verdict(limits=None, history=None, config=None, now=NOW, tz="UTC", **raw):
    """Run the plain-text and --json forms against the same state dir."""
    d = make_dir(limits, history, config, **raw)
    try:
        t = run_py([], d, now, tz)
        j = run_py(["--json"], d, now, tz)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return Result(t, j)


def gate(limits=None, config=None, now=NOW, tz="UTC", as_json=True, **raw):
    cfg = {"gate": config} if config is not None else None
    d = make_dir(limits, None, cfg, **raw)
    try:
        r = run_py(["--gate"] + (["--json"] if as_json else []), d, now, tz)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return r


def ratio(history=None, **raw):
    d = make_dir(None, history, **raw)
    try:
        return run_py(["--ratio"], d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def taipei(y, mo, d, h, mi=0):
    """Epoch seconds for a wall-clock time in Asia/Taipei (UTC+8, no DST)."""
    return int(datetime(y, mo, d, h, mi, tzinfo=timezone(timedelta(hours=8))).timestamp())


def guard(limits, stdin='{"session_id":"s1"}', config=None, pace=None, state_dir=None,
          now=NOW, tz="UTC", raw_limits=None):
    """Run budget-guard.cjs. Returns (CompletedProcess, parsed additionalContext or None, dir)."""
    d = state_dir or make_dir(limits, None, config, raw_limits=raw_limits)
    if pace is not None:
        _write(os.path.join(d, "pace.json"), json.dumps(pace))
    env = dict(os.environ, TZ=tz)
    env.pop("CLAUDE_PACER_DIR", None)
    if now is not None:
        env["USAGE_VERDICT_NOW"] = str(now)
    r = subprocess.run(["node", GUARD, "--dir", d], input=stdin, capture_output=True,
                       text=True, encoding="utf-8", env=env, timeout=30)
    ctx = None
    if r.stdout.strip():
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    return r, ctx, d
