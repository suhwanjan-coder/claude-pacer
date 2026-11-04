"""Missing / corrupt / wrongly-typed inputs. Contract from the docstring: exit 3 = no data,
and a hook-facing tool must never emit a traceback (exit 1 would be misread as PACE)."""
import json
import pytest
from helpers import NOW, five_hour, seven_day, verdict, make_dir, run_py, ratio, gate

NO_DATA = 3


def test_state_dir_missing_entirely():
    r = run_py([], "/nonexistent/pacer-dir")
    assert (r.returncode, r.stdout.startswith("NO-DATA")) == (NO_DATA, True)
    assert r.stderr == ""


@pytest.mark.parametrize("raw", ["", "   \n", "{", '{"five_hour": {"used_perc', "not json at all", "\x00\x01\x02"])
def test_limits_unreadable_or_truncated(raw):
    r = verdict(raw_limits=raw)
    assert r.code == NO_DATA
    assert r.text.startswith("NO-DATA")
    assert r.stderr == ""


def test_limits_binary_garbage():
    r = verdict(raw_limits=b"\xff\xfe\x00\x80\x81")
    assert r.code == NO_DATA and r.stderr == ""


@pytest.mark.parametrize("limits", [
    {}, {"five_hour": None}, {"five_hour": {}},
    {"five_hour": {"used_percentage": None, "resets_at": NOW + 3600}},
    {"five_hour": {"used_percentage": 50, "resets_at": None}},
    {"five_hour": {"used_percentage": 50, "resets_at": 0}},
    {"five_hour": {"used_percentage": 50}},
    {"five_hour": {"resets_at": NOW + 3600}},
    {"seven_day": seven_day(50, 48)},
])
def test_missing_fields_are_no_data(limits):
    r = verdict(limits)
    assert r.code == NO_DATA, r.text
    assert r.text.startswith("NO-DATA")
    assert r.stderr == ""


def test_missing_history_is_fine():
    r = verdict({"five_hour": five_hour(40, 150)})
    assert r.code == 0 and r.stderr == ""


def test_corrupt_history_lines_skipped():
    hist = '{"ts": 1}\nGARBAGE\n\n{"ts": "x", "pct": 3}\n[1,2]\n'
    r = verdict({"five_hour": five_hour(40, 150)}, raw_history=hist)
    assert r.code == 0 and r.stderr == ""


# ---- wrong types: expected = clean NO-DATA (3), never a traceback ----
BAD_TYPES = [
    pytest.param({"five_hour": {"used_percentage": "50", "resets_at": NOW + 3600}}, id="pct-string"),
    pytest.param({"five_hour": {"used_percentage": 50, "resets_at": "soon"}}, id="resets-string"),
    pytest.param({"five_hour": {"used_percentage": [50], "resets_at": NOW + 3600}}, id="pct-list"),
    pytest.param({"five_hour": [1, 2]}, id="block-is-list"),
    pytest.param({"five_hour": "x"}, id="block-is-string"),
]


@pytest.mark.parametrize("limits", BAD_TYPES)
def test_wrongly_typed_five_hour_is_clean_no_data(limits):
    r = verdict(limits)
    assert r.code == NO_DATA
    assert r.stderr == ""


@pytest.mark.parametrize("raw", ["[]", "null", "42", '"str"', "true"])
def test_non_object_limits_json_is_clean_no_data(raw):
    r = verdict(raw_limits=raw)
    assert r.code == NO_DATA
    assert r.stderr == ""


def test_seven_day_wrong_type_is_tolerated_or_ignored():
    # 7d block of the wrong type must not take down the 5h verdict
    r = verdict({"five_hour": five_hour(40, 150), "seven_day": [1]})
    assert r.stderr == ""


def test_limits_with_bom_is_readable_like_gate_does():
    raw = b"\xef\xbb\xbf" + json.dumps({"five_hour": five_hour(40, 150)}).encode()
    assert verdict(raw_limits=raw).code == 0


def test_gate_reads_bom_limits_for_comparison():
    raw = b"\xef\xbb\xbf" + json.dumps({"five_hour": five_hour(99, 150)}).encode()
    r = gate(raw_limits=raw, config={"peak_hour_gate_enabled": False})
    assert r.returncode == 10  # 99% used vs ~50% allowance -> BLOCK (so the BOM was parsed)


# ---- gate with garbage ----
def test_gate_corrupt_limits_follows_fail_open_policy():
    r = gate(raw_limits="{oops", config={"peak_hour_gate_enabled": False})
    assert r.returncode == 0 and "fail-open" in r.stdout
    r = gate(raw_limits="{oops", config={"peak_hour_gate_enabled": False, "fail_open_when_no_data": False})
    assert r.returncode == 10 and "fail-closed" in r.stdout


@pytest.mark.parametrize("raw", ["null", "[]", "7"])
def test_gate_non_object_limits(raw):
    r = gate(raw_limits=raw, config={"peak_hour_gate_enabled": False})
    assert r.returncode in (0, 10) and r.stderr == ""


def test_gate_string_pct():
    r = gate({"five_hour": {"used_percentage": "50", "resets_at": NOW + 7200}},
             config={"peak_hour_gate_enabled": False})
    assert r.returncode in (0, 10) and r.stderr == ""


# ---- ratio ----
def test_ratio_no_history():
    r = ratio()
    assert r.returncode == NO_DATA and r.stdout.startswith("NO-DATA")


def test_ratio_empty_and_corrupt_history_insufficient():
    r = ratio(raw_history="garbage\n\n{bad\n")
    assert r.returncode == NO_DATA and "INSUFFICIENT" in r.stdout


def _pair(rs, sdr, p0, p1, s0, s1):
    return [{"ts": 1000, "pct": p0, "sd_pct": s0, "resets_at": rs, "sd_resets": sdr},
            {"ts": 2000, "pct": p1, "sd_pct": s1, "resets_at": rs, "sd_resets": sdr}]


def test_ratio_computed_from_paired_deltas():
    # three windows: d5h = 30+30+30 = 90, d7d = 3+3+3 = 9 -> ratio 10.0
    h = []
    for i in range(3):
        h += _pair(100 + i, 900, 0, 30, 10 + 3 * i, 13 + 3 * i)
    r = ratio(h)
    assert r.returncode == 0, r.stdout
    assert "cap_7d/cap_5h ~= 10.0" in r.stdout
    assert "3 window-pairs" in r.stdout


def test_ratio_skips_decreasing_and_incomplete_groups():
    h = _pair(1, 9, 0, 30, 0, 6)  # valid: d5=30 d7=6
    h += _pair(2, 9, 50, 10, 0, 6)  # 5h decreased -> skipped
    h += [{"ts": 1, "pct": 5, "resets_at": 3}]  # no sd_pct -> filtered
    h += [{"ts": 1, "pct": 5, "sd_pct": 1, "resets_at": None, "sd_resets": 4},
          {"ts": 2, "pct": 9, "sd_pct": 2, "resets_at": None, "sd_resets": 4}]  # None key -> skipped
    r = ratio(h)
    assert r.returncode == 0
    assert "cap_7d/cap_5h ~= 5.0" in r.stdout and "1 window-pairs" in r.stdout


def test_ratio_below_5pp_is_insufficient_not_divide_by_zero():
    r = ratio(_pair(1, 9, 0, 30, 0, 4))
    assert r.returncode == NO_DATA and "INSUFFICIENT" in r.stdout
    r = ratio(_pair(1, 9, 0, 30, 5, 5))  # d7 == 0
    assert r.returncode == NO_DATA and r.stderr == ""


def test_ratio_sample_without_ts():
    h = _pair(1, 9, 0, 30, 0, 6) + [{"pct": 5, "sd_pct": 1, "resets_at": 1, "sd_resets": 9}]
    r = ratio(h)
    assert r.returncode in (0, NO_DATA) and r.stderr == ""
