"""usage_verdict.py: thresholds, boundaries, reset arithmetic, 7d logic (synthetic data)."""
import pytest
from helpers import NOW, MIN, H, DAY, five_hour, seven_day, verdict

GO, PACE, STOP = 0, 1, 2


def v(pct, remain_min=290, sd=None, **kw):
    """Default remain=290 -> only 10 min into the window, so the projection is ignored
    ('early') and only the pct thresholds decide."""
    limits = {"five_hour": five_hour(pct, remain_min),
              "seven_day": sd or seven_day(10, 5 * 24)}
    return verdict(limits, **kw)


# ---- threshold boundaries (SOFT=85, HARD=93 defaults) ----
@pytest.mark.parametrize("pct,code,word", [
    (0, GO, "GO"), (84, GO, "GO"), (84.99, GO, "GO"),
    (85, PACE, "PACE"), (85.01, PACE, "PACE"),
    (92, PACE, "PACE"), (92.99, PACE, "PACE"),
    (93, STOP, "STOP"), (93.01, STOP, "STOP"),
    (100, STOP, "STOP"),
])
def test_threshold_boundaries(pct, code, word):
    r = v(pct)
    assert r.code == code, r.text
    assert r.text.startswith(word + " "), r.text
    assert r.j["verdict"] == word


def test_pct_zero_and_hundred_exact():
    assert v(0).code == GO
    assert "0%" in v(0).text
    assert v(100).code == STOP


def test_pct_above_100_still_stop():
    assert v(140).code == STOP


def test_negative_pct_does_not_crash():
    r = v(-5)
    assert r.code == GO
    assert r.stderr == ""


# ---- near-reset exemption (NEAR_RESET=20 min) ----
@pytest.mark.parametrize("pct,remain,code", [
    (94, 20, PACE),   # hard + near -> softened to PACE
    (94, 21, STOP),   # one minute outside the near window
    (90, 20, GO),     # soft + near -> exempt
    (90, 21, PACE),
    (85, 0, GO),      # 0 min left but not yet reset
])
def test_near_reset_boundary(pct, remain, code):
    r = v(pct, remain_min=remain)
    assert r.code == code, r.text


def test_near_reset_uses_truncated_minutes():
    # 20 min 59 s left -> int(1259/60) == 20 -> still "near"
    r = verdict({"five_hour": {"used_percentage": 90, "resets_at": NOW + 20 * MIN + 59},
                 "seven_day": seven_day(10, 100)})
    assert r.code == GO, r.text
    assert r.j["five_hour"]["remain_min"] == 20


# ---- reset arithmetic ----
def test_reset_exactly_now_counts_as_reset():
    r = verdict({"five_hour": {"used_percentage": 99, "resets_at": NOW}})
    assert r.code == GO
    assert "ALREADY RESET" in r.text
    assert r.j["five_hour"]["state"] == "RESET"


def test_reset_one_second_in_future_is_not_reset():
    r = verdict({"five_hour": {"used_percentage": 99, "resets_at": NOW + 1}})
    assert "ALREADY RESET" not in r.text
    assert r.code == PACE  # hard but near-reset softened


def test_reset_passed_long_ago():
    r = verdict({"five_hour": {"used_percentage": 100, "resets_at": NOW - 30 * DAY}})
    assert r.code == GO
    assert "ALREADY RESET" in r.text


def test_reset_far_future_does_not_crash_and_stop_still_rules():
    # resets_at 10 days ahead is impossible for a 5h window; must not divide by zero / crash.
    r = verdict({"five_hour": {"used_percentage": 10, "resets_at": NOW + 10 * DAY}})
    assert r.stderr == ""
    assert r.code == GO
    r = verdict({"five_hour": {"used_percentage": 94, "resets_at": NOW + 10 * DAY}})
    assert r.code == STOP


def test_window_just_started_no_division_by_zero():
    # resets_at == now + 5h exactly -> elapsed 0 min (clamped to 1)
    for pct in (0, 1, 50):
        r = verdict({"five_hour": {"used_percentage": pct, "resets_at": NOW + 5 * H}})
        assert r.stderr == "", r.stderr
        assert r.code == GO
        assert r.j["five_hour"]["pace"] >= 0


# ---- early-window projection guard ----
def test_projection_ignored_first_20_min_but_counts_after():
    # 1% after 1 min projects ~300% but must stay GO
    r = verdict({"five_hour": {"used_percentage": 1, "resets_at": NOW + 299 * MIN}})
    assert r.code == GO
    assert r.j["projected_at_reset"] >= 100
    assert "not acted on yet" in r.text
    # 10% after 25 min => pace 1.2, projects 120% -> PACE
    r = verdict({"five_hour": {"used_percentage": 10, "resets_at": NOW + 275 * MIN}})
    assert r.code == PACE


def test_pace_boundary_0_9():
    # elapsed 150/300 -> need pct 45 for pace 0.90 (projection 90 <100 => GO regardless)
    r = verdict({"five_hour": five_hour(45, 150)})
    assert r.code == GO
    assert r.j["five_hour"]["pace"] == 0.9
    # elapsed 240 min, pct 72 -> avg 0.3/min, remain 60 -> projected 90 => GO
    assert verdict({"five_hour": five_hour(72, 60)}).code == GO
    # pct 80 -> projected 100, pace 1.0 -> PACE even though pct < soft
    r = verdict({"five_hour": five_hour(80, 60)})
    assert r.code == PACE
    assert "projected" in r.text


# ---- 7d logic ----
def test_7d_reset_before_5h_ends_is_ignored():
    r = verdict({"five_hour": five_hour(30, 120), "seven_day": seven_day(99, 1)})
    assert r.j["seven_day"]["binding"] is False
    assert "IGNORE" in r.text


def test_7d_reset_equal_to_5h_reset_is_ignored():
    r = verdict({"five_hour": five_hour(30, 120), "seven_day": {"used_percentage": 99, "resets_at": NOW + 120 * MIN}})
    assert r.j["seven_day"]["binding"] is False


def test_7d_already_reset_is_ignored():
    r = verdict({"five_hour": five_hour(30, 120), "seven_day": {"used_percentage": 99, "resets_at": NOW - 5}})
    assert r.j["seven_day"]["binding"] is False
    assert "already reset" in r.text


@pytest.mark.parametrize("sd_pct,binding", [(94, False), (94.99, False), (95, True), (100, True)])
def test_7d_binding_boundary(sd_pct, binding):
    r = verdict({"five_hour": five_hour(30, 120), "seven_day": seven_day(sd_pct, 48)})
    assert r.j["seven_day"]["binding"] is binding
    assert ("CAUTION: 7d is binding" in r.text) is binding


def test_7d_missing_reports_no_data():
    r = verdict({"five_hour": five_hour(30, 120)})
    assert r.code == GO
    assert "[7d: no data]" in r.text


def test_windows_independent_7d_never_changes_verdict():
    low = verdict({"five_hour": five_hour(30, 120), "seven_day": seven_day(1, 48)})
    high = verdict({"five_hour": five_hour(30, 120), "seven_day": seven_day(99, 48)})
    assert low.code == high.code == GO


# ---- config thresholds ----
def test_config_overrides_thresholds_flat_and_guard_block():
    for cfg in ({"soft_pct": 50, "hard_pct": 60}, {"guard": {"soft_pct": 50, "hard_pct": 60}}):
        assert v(49, config=cfg).code == GO
        assert v(50, config=cfg).code == PACE
        assert v(60, config=cfg).code == STOP
        assert "hard 60%" in v(60, config=cfg).text


def test_config_near_reset_override():
    cfg = {"guard": {"near_reset_min": 5}}
    assert v(94, remain_min=6, config=cfg).code == STOP
    assert v(94, remain_min=5, config=cfg).code == PACE


@pytest.mark.parametrize("raw", ["", "{not json", "[]", "null", '{"guard": null}', '{"guard": 5}'])
def test_broken_config_falls_back_to_defaults(raw):
    r = v(93, raw_config=raw)
    assert r.stderr == ""
    assert r.code == STOP


@pytest.mark.xfail(strict=True, reason="BUG B3: verdict reads config.json without utf-8-sig, so a BOM'd config "
                   "(Windows editors) is silently ignored while --gate honours it")
def test_config_with_utf8_bom_is_honoured():
    r = v(86, raw_config=b'\xef\xbb\xbf{"soft_pct": 99}')
    assert r.code == GO  # soft_pct 99 should apply
