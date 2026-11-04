"""Asia/Taipei (UTC+8, no DST), midnight crossings, and the --gate preflight."""
import json
import pytest
from helpers import NOW, MIN, H, DAY, five_hour, seven_day, verdict, gate, taipei

# ---- clock(): local-day logic, run under TZ=Asia/Taipei ----
TPE = "Asia/Taipei"


def test_clock_same_day_in_taipei():
    now = taipei(2026, 11, 4, 22, 0)
    r = verdict({"five_hour": {"used_percentage": 30, "resets_at": taipei(2026, 11, 4, 23, 30)}}, now=now, tz=TPE)
    assert r.j["now"] == "22:00"
    assert r.j["five_hour"]["resets_at"] == "23:30"


def test_clock_crossing_midnight_in_taipei_shows_date():
    now = taipei(2026, 11, 4, 23, 50)
    r = verdict({"five_hour": {"used_percentage": 30, "resets_at": taipei(2026, 11, 5, 0, 20)}}, now=now, tz=TPE)
    assert r.j["five_hour"]["resets_at"] == "11-05 00:20"
    assert r.j["five_hour"]["remain_min"] == 30


def test_clock_same_epoch_differs_by_host_timezone():
    # 23:50 TPE == 15:50 UTC. Same instant; the displayed day/clock must follow the host TZ.
    now = taipei(2026, 11, 4, 23, 50)
    resets = now + 30 * MIN
    lim = {"five_hour": {"used_percentage": 30, "resets_at": resets}}
    assert verdict(lim, now=now, tz=TPE).j["five_hour"]["resets_at"] == "11-05 00:20"
    assert verdict(lim, now=now, tz="UTC").j["five_hour"]["resets_at"] == "16:20"


def test_clock_7d_note_across_month_boundary():
    now = taipei(2026, 11, 29, 23, 0)
    lim = {"five_hour": {"used_percentage": 30, "resets_at": now + 2 * H},
           "seven_day": {"used_percentage": 50, "resets_at": taipei(2026, 12, 3, 8, 0)}}
    r = verdict(lim, now=now, tz=TPE)
    assert "(resets 12-03 08:00)" in r.text


def test_reset_arithmetic_unaffected_by_timezone():
    now = taipei(2026, 11, 4, 12, 0)
    lim = {"five_hour": {"used_percentage": 99, "resets_at": now - 1}}
    for tz in (TPE, "UTC", "America/Los_Angeles"):
        assert verdict(lim, now=now, tz=tz).code == 0


# ---- gate: peak hours use a fixed offset (tz_offset_hours), independent of host TZ ----
PEAK_OFF = {"peak_hour_gate_enabled": False}


@pytest.mark.parametrize("hh,mm,blocked", [
    (20, 59, False), (21, 0, True), (23, 59, True), (0, 0, True),
    (1, 59, True), (2, 0, False), (12, 0, False),
])
@pytest.mark.parametrize("host_tz", ["UTC", "Asia/Taipei", "America/New_York"])
def test_gate_peak_window_is_taipei_regardless_of_host_tz(hh, mm, blocked, host_tz):
    r = gate(config={}, now=taipei(2026, 11, 4, hh, mm), tz=host_tz)
    assert (r.returncode == 10) is blocked, r.stdout
    assert json.loads(r.stdout)["gate"] == ("BLOCK" if blocked else "ALLOW")


def test_gate_cross_midnight_dates_do_not_matter():
    # 01:59 on the 5th is still the 4th's peak; 02:00 is out.
    assert gate(config={}, now=taipei(2026, 11, 5, 1, 59)).returncode == 10
    assert gate(config={}, now=taipei(2026, 11, 5, 2, 0)).returncode == 0


def test_gate_custom_offset_and_non_wrapping_peak():
    # UTC offset 0, peak 09:00-17:00 (start < end, no wrap)
    cfg = {"tz_offset_hours": 0, "peak_start": "09:00", "peak_end": "17:00"}
    at = lambda h, m: NOW - 10 * H + h * H + m * 60  # NOW is 10:00 UTC -> base 00:00 UTC
    assert gate(config=cfg, now=at(8, 59)).returncode == 0
    assert gate(config=cfg, now=at(9, 0)).returncode == 10
    assert gate(config=cfg, now=at(16, 59)).returncode == 10
    assert gate(config=cfg, now=at(17, 0)).returncode == 0


def test_gate_fractional_offset():
    cfg = {"tz_offset_hours": 5.5, "peak_start": "10:00", "peak_end": "11:00"}  # NOW=10:00 UTC = 15:30 IST
    assert gate(config=cfg).returncode == 0
    cfg["peak_start"], cfg["peak_end"] = "15:00", "16:00"
    assert gate(config=cfg).returncode == 10


def test_gate_equal_start_end_blocks_all_day_observed():
    # Observed (not asserted to be intended): start == end takes the wrap branch
    # (hhmm >= start or hhmm < end), which is true for every time -> permanent BLOCK.
    cfg = {"peak_start": "21:00", "peak_end": "21:00"}
    for h in (0, 12, 21, 23):
        assert gate(config=cfg, now=taipei(2026, 11, 4, h, 0)).returncode == 10


# ---- gate: allowance arithmetic ----
def _gate5(pct, remain_min, **kw):
    return gate({"five_hour": five_hour(pct, remain_min)}, config=PEAK_OFF, **kw)


@pytest.mark.parametrize("pct,code", [(49.99, 0), (50, 0), (50.01, 10)])
def test_gate_5h_allowance_boundary(pct, code):
    assert _gate5(pct, 150).returncode == code


def test_gate_zero_and_full_usage():
    assert _gate5(0, 150).returncode == 0
    assert _gate5(100, 150).returncode == 10


def test_gate_100pct_one_second_before_reset_still_blocks():
    # allowance = elapsed% (99.99) < 100 used -> BLOCK; by design the gate never lets a full 5h through
    r = gate({"five_hour": {"used_percentage": 100, "resets_at": NOW + 1}}, config=PEAK_OFF)
    assert r.returncode == 10


def test_gate_negative_pct_allowed():
    assert _gate5(-3, 150).returncode == 0


def test_gate_window_just_started_clamps_elapsed():
    # resets_at far in future -> elapsed clamped to 0 -> ceiling = -headroom
    r = gate({"five_hour": {"used_percentage": 0, "resets_at": NOW + 99 * DAY}}, config=PEAK_OFF)
    assert r.returncode == 0  # 0 > 0 is False
    r = gate({"five_hour": {"used_percentage": 1, "resets_at": NOW + 99 * DAY}}, config=PEAK_OFF)
    assert r.returncode == 10


def test_gate_7d_blocks_even_when_5h_is_fine():
    r = gate({"five_hour": five_hour(1, 150), "seven_day": seven_day(60, 84)}, config=PEAK_OFF)
    assert r.returncode == 10 and "7d" in json.loads(r.stdout)["reason"]


def test_gate_output_is_one_line_and_clean_stderr():
    r = _gate5(10, 150, as_json=False)
    assert r.stderr == "" and len(r.stdout.splitlines()) == 1
