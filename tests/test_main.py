"""HoverCargo-X tests — physics and gates, all against analytic values."""
import math

import main as cx


# --- lift model -------------------------------------------------------------
def test_hover_thrust_is_mg():
    assert abs(cx.hover_thrust_n(320.0) - 320.0 * cx.G) < 1e-6


def test_thrust_per_motor_sums_to_total():
    total = cx.thrust_per_motor_n(320.0) * cx.VEHICLE["motors"]
    assert abs(total - cx.hover_thrust_n(320.0)) < 1e-6


def test_disk_area():
    assert abs(cx.disk_area_m2() - 6 * math.pi * 0.90 ** 2) < 1e-9


def test_mtom():
    assert cx.mtom_kg(200.0) == 320.0
    assert cx.mtom_kg(0.0) == 120.0


def test_momentum_power_formula():
    expected = 4000.0 ** 1.5 / math.sqrt(2 * cx.RHO_SEA * 15.279)
    assert abs(cx.ideal_hover_power_w(4000.0, cx.RHO_SEA, 15.279) - expected) < 1e-6


def test_power_scales_with_T_3_2():
    p1 = cx.ideal_hover_power_w(2000.0, cx.RHO_SEA, 15.279)
    p2 = cx.ideal_hover_power_w(4000.0, cx.RHO_SEA, 15.279)
    assert abs(p2 / p1 - 2 ** 1.5) < 1e-9


def test_power_scales_with_inv_sqrt_A():
    a = cx.disk_area_m2()
    p_full = cx.ideal_hover_power_w(4000.0, cx.RHO_SEA, a)
    p_quarter = cx.ideal_hover_power_w(4000.0, cx.RHO_SEA, a / 4.0)
    assert abs(p_quarter / p_full - 2.0) < 1e-9


def test_hover_power_uses_fom():
    expected = cx.ideal_hover_power_w(cx.hover_thrust_n(320.0)) / 0.75
    assert abs(cx.hover_power_w(320.0) - expected) < 1e-6


def test_payload_classes_monotone():
    table = {r["payload_kg"]: r for r in cx.payload_class_table()}
    assert table[10.0]["hover_power_kw"] < table[50.0]["hover_power_kw"]
    assert table[50.0]["hover_power_kw"] < table[200.0]["hover_power_kw"]
    assert table[10.0]["endurance_min"] > table[200.0]["endurance_min"]
    assert all(r["thrust_margin"] > 1.5 for r in table.values())


# --- slung-load dynamics ------------------------------------------------------
def test_pendulum_frequency_analytic():
    expected = math.sqrt(cx.G / 3.0) / (2 * math.pi)
    assert abs(cx.pendulum_frequency_hz(3.0) - expected) < 1e-12


def test_pendulum_frequency_falls_with_length():
    assert cx.pendulum_frequency_hz(6.0) < cx.pendulum_frequency_hz(3.0)
    assert cx.pendulum_frequency_hz(3.0) < cx.pendulum_frequency_hz(1.5)


def test_unshaped_swing_residual_is_real():
    _, residual = cx.swing_sim(shaped=False)
    assert residual > 2.0


def test_input_shaping_cancels_swing():
    _, res_raw = cx.swing_sim(shaped=False)
    _, res_shaped = cx.swing_sim(shaped=True)
    assert res_shaped < 0.25 * res_raw


def test_pd_feedback_damps_swing():
    max_raw, res_raw = cx.swing_sim(shaped=False)
    max_pd, res_pd = cx.swing_sim(shaped=False, pd_gain=0.9)
    assert max_pd < max_raw
    assert res_pd < res_raw


def test_rigid_mount_load_factor():
    assert abs(cx.rigid_mount_load_factor(cx.G) - 1.0) < 1e-12
    assert abs(cx.rigid_mount_load_factor(1.2) - 1.2 / cx.G) < 1e-12


def test_rigid_mount_cg_shift():
    assert abs(cx.rigid_mount_cg_shift_m(200.0) - 0.30 * 200.0 / 320.0) < 1e-9


# --- energy / range -----------------------------------------------------------
def test_warehouse_hop_planned_and_reserve_ok():
    hop = cx.plan_trip(50.0, 38.0)
    assert hop["verdict"] == "PLANNED"
    assert hop["soc_end"] >= cx.RESERVE_SOC


def test_trip_energy_audit_consistent():
    energy, _ = cx.trip_energy_kwh(50.0, 38.0)
    hop = cx.plan_trip(50.0, 38.0)
    assert abs(hop["soc_end"] - (0.95 - energy / 30.0)) < 0.01


def test_overrange_trip_refused():
    assert cx.plan_trip(200.0, 120.0)["verdict"] == "REFUSED"


def test_range_falls_with_payload():
    rt = {r["payload_kg"]: r["max_range_km"] for r in cx.range_table()}
    assert rt[10.0] > rt[50.0] > rt[200.0]


def test_max_range_lands_on_reserve():
    rt = {r["payload_kg"]: r["max_range_km"] for r in cx.range_table()}
    assert abs(cx.plan_trip(50.0, rt[50.0])["soc_end"] - cx.RESERVE_SOC) < 0.01


# --- wind limits ---------------------------------------------------------------
def test_wind_limits_rise_with_class():
    assert cx.max_wind_mps(10.0) < cx.max_wind_mps(50.0)
    assert cx.max_wind_mps(50.0) < cx.max_wind_mps(200.0)


def test_wind_limit_vehicle_cap():
    assert cx.max_wind_mps(200.0) == cx.VEHICLE_WIND_CAP_MPS


def test_swing_at_wind_limit_is_the_cap():
    v = cx.max_wind_mps(50.0)
    assert abs(cx.steady_swing_deg(v, 50.0) - cx.MAX_SWING_DEG) < 0.1


def test_wind_verdicts():
    assert cx.wind_verdict(50.0, 5.0) == "GO"
    assert cx.wind_verdict(10.0, cx.max_wind_mps(10.0) + 1.0) == "NO-GO"


# --- governor + interlocks ------------------------------------------------------
def test_governor_clamps_yard():
    gc = cx.governor_clamp("YARD", 20.0, 80.0)
    assert gc["speed_mps"] == 8.0 and gc["alt_m"] == 40.0 and gc["speed_clamped"]


def test_governor_absolute_ceiling():
    gc = cx.governor_clamp("TEST_AREA", 99.0, 999.0)
    assert gc["alt_m"] == cx.ABSOLUTE_CEILING_M


def test_interlocks_nominal_cleared():
    assert cx.load_interlocks(cx.NOMINAL_LOAD_STATE)["verdict"] == "CLEARED"


def test_overload_refused():
    state = dict(cx.NOMINAL_LOAD_STATE, measured_kg=240.0, declared_kg=240.0)
    assert cx.load_interlocks(state)["verdict"] == "REFUSED"


def test_cg_out_of_box_refused():
    state = dict(cx.NOMINAL_LOAD_STATE, cg_offset_m=0.40)
    assert cx.load_interlocks(state)["verdict"] == "REFUSED"


def test_unlatched_refused():
    state = dict(cx.NOMINAL_LOAD_STATE, latch_confirmed=False)
    assert cx.load_interlocks(state)["verdict"] == "REFUSED"


def test_sway_at_pickup_refused():
    state = dict(cx.NOMINAL_LOAD_STATE, sway_deg=3.5)
    assert cx.load_interlocks(state)["verdict"] == "REFUSED"


# --- motor-out -------------------------------------------------------------------
def test_motor_out_light_continues():
    assert cx.motor_out_check(50.0, motors_out=1)["verdict"] == "CONTINUE"


def test_motor_out_heavy_lands_now():
    assert cx.motor_out_check(200.0, motors_out=1)["verdict"] == "LAND_NOW"


def test_motor_out_two_heavy_emergency():
    mo = cx.motor_out_check(200.0, motors_out=2)
    assert mo["verdict"] == "EMERGENCY_DESCENT"
    assert abs(mo["margin"] - 4 * 1200.0 / cx.hover_thrust_n(320.0)) < 0.01


# --- fleet scheduling --------------------------------------------------------------
def test_fleet_no_pad_conflicts():
    slots = cx.schedule_fleet(cx.FLEET_REQUESTS)
    assert not cx.slots_conflict(slots)


def test_fleet_order_deterministic():
    slots = cx.schedule_fleet(cx.FLEET_REQUESTS)
    assert [s["vehicle"] for s in slots] == ["HCX-1", "HCX-2", "HCX-3"]
    assert cx.schedule_fleet(cx.FLEET_REQUESTS) == slots


# --- classifier -----------------------------------------------------------------------
def test_knn_go_case():
    assert cx.knn_classify(0.25, 4.0, 20.0, 0.90) == "go"


def test_knn_nogo_case():
    assert cx.knn_classify(1.0, 13.0, 50.0, 0.40) == "no-go"


def test_knn_training_accuracy():
    assert cx.knn_training_accuracy() >= 0.85


# --- seats + HOLD gates ------------------------------------------------------------------
def test_carbon4_trace_deterministic():
    a = cx.carbon4_decide({"live_action": False, "interlocks_pass": True})
    b = cx.carbon4_decide({"live_action": False, "interlocks_pass": True})
    assert len(a["trace_sha256"]) == 64
    assert a["trace_sha256"] == b["trace_sha256"]


def test_carbon4_tightens_only():
    ok = cx.carbon4_decide({"live_action": False, "interlocks_pass": True})
    live = cx.carbon4_decide({"live_action": True, "interlocks_pass": True})
    refused = cx.carbon4_decide({"live_action": False, "refused": True})
    assert ok["verdict"] == "PROCEED"
    assert live["verdict"] == "HOLD"
    assert refused["verdict"] == "HOLD"
    assert ok["tighten_only"] is True


def test_muse_never_overrides():
    assert cx.MUSE_ADVISOR["can_override_hold"] is False


def test_flow_plan_only():
    fp = cx.flow_plan()
    assert fp["rendered"] is False and fp["live"] == 0


def test_hold_gates_exactly_14():
    assert len(cx.HOLD_GATES) == 14
    for gate in ("live_flight", "cargo_ops_live", "hazmat_cargo_live",
                 "disable_load_interlocks", "exceed_payload_live",
                 "remove_human_override", "bvlos_live"):
        assert gate in cx.HOLD_GATES


def test_hold_demo_exits_2(capsys):
    assert cx.hold_demo("live_flight") == 2
    out = capsys.readouterr().out
    assert "HOLD" in out


def test_checks_all_pass():
    checks, passed = cx.run_checks()
    assert passed == len(checks)
