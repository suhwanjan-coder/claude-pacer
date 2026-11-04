"""budget-guard.cjs hook (needs node). It must always exit 0 and print either nothing or
one JSON object with hookSpecificOutput.additionalContext."""
import json
import os
import shutil
import pytest
from helpers import NOW, MIN, H, DAY, HAS_NODE, five_hour, seven_day, guard, make_dir, verdict

pytestmark = pytest.mark.skipif(not HAS_NODE, reason="node not installed")


def lim(pct, remain_min, sd=None, ts=NOW):
    return {"ts": ts * 1000, "five_hour": five_hour(pct, remain_min), "seven_day": sd or seven_day(30, 100)}


def msg(pct, remain_min, **kw):
    r, ctx, d = guard(lim(pct, remain_min), **kw)
    shutil.rmtree(d, ignore_errors=True)
    assert r.returncode == 0 and r.stderr == ""
    return ctx


# ---- silent cases ----
def test_silent_below_soft():
    assert msg(84, 120) is None
    assert msg(0, 120) is None


def test_silent_when_window_already_reset():
    assert msg(99, 0) is None
    r, ctx, d = guard({"ts": NOW * 1000, "five_hour": {"used_percentage": 99, "resets_at": NOW - 60}})
    assert ctx is None and r.returncode == 0


def test_silent_when_data_stale():
    # default stale_min = 15 min
    r, ctx, d = guard(lim(99, 120, ts=NOW - 16 * 60))
    assert ctx is None
    r, ctx, d = guard(lim(99, 120, ts=NOW - 14 * 60))
    assert ctx is not None


def test_stale_boundary_exactly_15_min_still_fresh():
    r, ctx, d = guard(lim(99, 120, ts=NOW - 15 * 60))
    assert ctx is not None  # `>` not `>=`


@pytest.mark.parametrize("stdin", ["", "not json", "{", "null"])
def test_bad_stdin_is_silent_exit_0(stdin):
    r, ctx, d = guard(lim(99, 120), stdin=stdin)
    assert r.returncode == 0 and r.stderr == ""
    if stdin != "null":
        assert ctx is None


@pytest.mark.parametrize("raw", ["", "{", "null", "[]", "{}", '{"five_hour": null}'])
def test_missing_or_corrupt_limits_is_silent(raw):
    r, ctx, d = guard(None, raw_limits=raw)
    assert (r.returncode, ctx, r.stderr) == (0, None, "")


def test_limits_file_missing_entirely():
    d = make_dir()
    r, ctx, _ = guard(None, state_dir=d)
    assert (r.returncode, ctx) == (0, None)


@pytest.mark.parametrize("five", [
    {"used_percentage": "99", "resets_at": NOW + 3600},
    {"used_percentage": 99},
    {"used_percentage": 99, "resets_at": 0},
    {"resets_at": NOW + 3600},
])
def test_bad_five_hour_fields_silent(five):
    r, ctx, d = guard({"ts": NOW * 1000, "five_hour": five})
    assert (r.returncode, ctx, r.stderr) == (0, None, "")


# ---- levels ----
def test_soft_message():
    m = msg(86, 120)
    assert m.startswith("[budget-guard] 5h usage at 86%")
    assert "Start winding down" in m and "resets in 120min" in m


def test_soft_threshold_exact_85_fires_84_does_not():
    assert msg(85, 120) is not None
    assert msg(84.9, 120) is None


def test_hard_message_arms_resume_and_ends_turn():
    m = msg(94, 120)
    assert "WRAP UP NOW" in m and "CronCreate" in m and "END THE TURN" in m


def test_hard_threshold_exact_93():
    assert "WRAP UP NOW" in msg(93, 120)
    assert "WRAP UP NOW" not in msg(92.9, 120)


def test_near_reset_soft_is_silent_hard_is_softened():
    assert msg(90, 20) is None
    m = msg(94, 20)
    assert "NO wind-down needed" in m and "WRAP UP" not in m


def test_pct_100_and_above():
    assert "WRAP UP NOW" in msg(100, 120)
    assert "WRAP UP NOW" in msg(250, 120)


def test_remain_min_never_negative():
    # 1 s before reset -> remain rounds to 0, never "-1min"
    m = msg(94, 0.0166)
    assert "resets in 0min" in m or "NO wind-down" in m


# ---- config ----
def test_config_thresholds_flat_and_nested():
    for cfg in ({"soft_pct": 50, "hard_pct": 60}, {"guard": {"soft_pct": 50, "hard_pct": 60}}):
        assert msg(49, 120, config=cfg) is None
        assert "Start winding down" in msg(55, 120, config=cfg)
        assert "WRAP UP NOW" in msg(60, 120, config=cfg)


def test_config_corrupt_uses_defaults():
    d2 = make_dir(lim(94, 120), raw_config="{broken")
    r, ctx, _ = guard(None, state_dir=d2)
    assert ctx and "WRAP UP NOW" in ctx


def test_builtin_auto_continue_true_never_says_stop():
    cfg = {"guard": {"builtin_auto_continue": True}}
    assert msg(88, 120, config=cfg) is None                      # soft is silent
    m = msg(95, 120, config=cfg)                                 # hard: keep working
    assert "KEEP WORKING" in m and "Do NOT wind down" in m
    assert "END THE TURN" not in m and "WRAP UP NOW" not in m
    assert "launch NO new subagent waves" in m
    m = msg(95, 10, config=cfg)                                  # hard near reset
    assert "Do NOT end the turn early" in m


def test_builtin_auto_continue_default_is_false():
    assert "WRAP UP NOW" in msg(95, 120)


# ---- dedup / re-arm ----
def test_dedup_per_session_and_rearm():
    d = make_dir(lim(90, 120))
    first = guard(None, state_dir=d)[1]
    again = guard(None, state_dir=d)[1]
    other = guard(None, state_dir=d, stdin='{"session_id":"s2"}')[1]
    assert first and again is None and other  # same session silent, other session still warned
    # 11 min later (rearm_min = 10) the same session hears it again
    later = NOW + 11 * MIN
    with open(os.path.join(d, "limits.json"), "w") as f:
        json.dump(lim(90, 120 - 11, ts=later), f)
    assert guard(None, state_dir=d, now=later)[1]
    shutil.rmtree(d, ignore_errors=True)


def test_dedup_is_per_level():
    d = make_dir(lim(90, 120))
    assert guard(None, state_dir=d)[1]
    with open(os.path.join(d, "limits.json"), "w") as f:
        json.dump(lim(95, 120), f)
    assert "WRAP UP NOW" in guard(None, state_dir=d)[1]  # soft was warned; hard still fires


def test_new_window_resets_dedup_state():
    d = make_dir(lim(90, 120))
    assert guard(None, state_dir=d)[1]
    with open(os.path.join(d, "limits.json"), "w") as f:
        json.dump({"ts": NOW * 1000, "five_hour": five_hour(90, 200)}, f)  # different resets_at
    assert guard(None, state_dir=d)[1]


def test_corrupt_state_file_is_tolerated():
    d = make_dir(lim(90, 120))
    with open(os.path.join(d, "guard-state.json"), "w") as f:
        f.write("{garbage")
    r, ctx, _ = guard(None, state_dir=d)
    assert r.returncode == 0 and ctx


# ---- pace mode ----
def test_pace_mode_emits_below_soft_with_interval():
    d = make_dir(lim(40, 150))
    r, ctx, _ = guard(None, state_dir=d, pace={"on": True, "agents": 3})
    assert ctx.startswith("[pace] 5h 40%") and "~3 concurrent agents" in ctx
    assert guard(None, state_dir=d)[1] is None  # within pace_interval_min (15)


def test_pace_off_is_silent_below_soft():
    r, ctx, d = guard(lim(40, 150), pace={"on": False})
    assert ctx is None


# ---- 7d note ----
def test_7d_notes():
    def note(sd, remain=120):
        r, ctx, d = guard({"ts": NOW * 1000, "five_hour": five_hour(90, remain), "seven_day": sd})
        return ctx
    assert "binding, near cap" in note(seven_day(95, 48))
    assert "not near cap" in note(seven_day(94, 48))
    assert "IGNORE, not a constraint" in note(seven_day(99, 1))
    assert "7d already reset" in note({"used_percentage": 99, "resets_at": NOW - 1})
    assert "not near cap" in note(seven_day(50, 48))


# ---- hook event name ----
def test_event_name_echoed():
    r, ctx, d = guard(lim(95, 120), stdin='{"session_id":"s","hook_event_name":"PostToolUse"}')
    assert json.loads(r.stdout)["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


# ---- guard vs verdict consistency ----
@pytest.mark.parametrize("pct,remain_sec", [(90, 20 * 60), (90, 21 * 60), (94, 20 * 60), (94, 21 * 60)])
def test_guard_and_verdict_agree_on_near_reset_at_whole_minutes(pct, remain_sec):
    limits = {"ts": NOW * 1000, "five_hour": {"used_percentage": pct, "resets_at": NOW + remain_sec},
              "seven_day": seven_day(10, 100)}
    v = verdict(limits)
    r, ctx, d = guard(limits)
    guard_says_wind_down = bool(ctx) and ("Start winding down" in ctx or "WRAP UP NOW" in ctx)
    verdict_says_wind_down = v.code in (1, 2) and "near-reset" not in v.text and "worst case is a short pause" not in v.text
    assert guard_says_wind_down == verdict_says_wind_down, (v.text, ctx)


def test_guard_and_verdict_agree_inside_the_rounding_band():
    limits = {"ts": NOW * 1000, "five_hour": {"used_percentage": 90, "resets_at": NOW + 20 * MIN + 45},
              "seven_day": seven_day(10, 100)}
    v = verdict(limits)
    r, ctx, d = guard(limits)
    assert v.code == 0                 # verdict: near-reset exempt (remain_min == 20)
    assert ctx is None, ctx            # guard should agree and stay silent
