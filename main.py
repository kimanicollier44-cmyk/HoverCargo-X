#!/usr/bin/env python3
"""
HoverCargo-X — Heavy-Lift Hover Cargo Transport Lab
=====================================================
by Darion Kellar

SIM-ONLY educational lab for a heavy-lift hover cargo vehicle
(hexacopter, slung + rigid load options). Pure Python stdlib, ONE
main.py, zero network, zero dependencies. Nothing here controls real
hardware, flies a real vehicle, or lifts a real load — every live
action is behind a HOLD gate, and the HOLD always wins.

What is modelled, with real physics / engineering math:
  1. Lift model ............... momentum theory at cargo scale, disk
                                loading trade-off, P ~ T^1.5 / sqrt(A)
  2. Load dynamics ............ slung-load pendulum, PID + input-shaping
                                sway damping, rigid-mount comparison
  3. Energy / range ........... battery-vs-payload range table, 25% SoC
                                reserve rule, Porter Ranch -> LA hop audit
  4. Wind limits .............. cargo sail area -> max wind per class,
                                go / caution / no-go from a forecast value
  5. Load interlocks .......... weight, CG box, latch, sway, overload
                                REFUSED — X-family governor + interlocks
  6. Motor-out ................ one motor lost on a hex: thrust margin
                                recomputed, honest verdict per class
  7. Fleet note ............... 3 vehicles, one pad, deterministic
                                staggered schedule with no conflicts
  8. Regime classifier ........ stdlib kNN: go / caution / no-go
  9. Advisory seats ........... Carbon 4 decider (tighten-only), Muse
                                advisor (never overrides a HOLD), Flow
                                plan-only (rendered=False, live=0)

Run:
  python3 main.py            full demo + CHECKS, prints VERDICT
  python3 main.py demo       same as above
  python3 main.py checks     CHECKS only
  python3 main.py holds      list the 14 HOLD gates
  python3 main.py hold-demo  ask for a live flight -> HOLD, exit 2
"""

import hashlib
import math
import sys

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
G = 9.80665            # standard gravity, m/s^2
RHO_SEA = 1.225        # air density at sea level, kg/m^3
RESERVE_SOC = 0.25     # never plan a trip that lands below 25% SoC

# ---------------------------------------------------------------------------
# 1. Vehicle model — heavy-lift hexacopter (sim)
# ---------------------------------------------------------------------------
VEHICLE = {
    "name": "HoverCargo-X Hex (sim)",
    "empty_mass_kg": 120.0,       # airframe, motors, avionics, battery
    "battery_kwh": 30.0,          # pack energy, included in empty mass
    "usable_soc_frac": 0.80,      # planning uses the top 80% of the pack
    "motors": 6,                  # hexacopter
    "rotor_radius_m": 0.90,       # per rotor — big and slow, on purpose
    "motor_max_thrust_n": 1200.0,  # per motor bench rating (sim)
    "figure_of_merit": 0.75,
    "max_payload_kg": 200.0,
}

PAYLOAD_CLASSES = (10.0, 50.0, 200.0)   # kg — light / medium / heavy


def mtom_kg(payload_kg):
    """Maximum take-off mass for a given payload."""
    return VEHICLE["empty_mass_kg"] + payload_kg


def disk_area_m2():
    """Total rotor disk area across all rotors."""
    return VEHICLE["motors"] * math.pi * VEHICLE["rotor_radius_m"] ** 2


def hover_thrust_n(mass_kg):
    """Thrust needed to hover: T = m * g. Exact by definition."""
    return mass_kg * G


def thrust_per_motor_n(mass_kg):
    """Hover thrust shared evenly across all motors."""
    return hover_thrust_n(mass_kg) / VEHICLE["motors"]


def disk_loading_n_m2(mass_kg):
    return hover_thrust_n(mass_kg) / disk_area_m2()


def max_total_thrust_n(motors_up=None):
    if motors_up is None:
        motors_up = VEHICLE["motors"]
    return motors_up * VEHICLE["motor_max_thrust_n"]


def thrust_margin(mass_kg, motors_up=None):
    """Max available thrust / hover thrust. 1.0 = barely hovering."""
    return max_total_thrust_n(motors_up) / hover_thrust_n(mass_kg)


# ---------------------------------------------------------------------------
# 1b. Momentum-theory power — P = T^(3/2) / sqrt(2 * rho * A) / FoM
#     This one formula is the whole cargo story: power grows with the
#     1.5 power of weight and falls with the square root of disk area,
#     which is why heavy-lift rotors are BIG and SLOW.
# ---------------------------------------------------------------------------
def ideal_hover_power_w(thrust_n, rho=RHO_SEA, area_m2=None):
    if area_m2 is None:
        area_m2 = disk_area_m2()
    return thrust_n ** 1.5 / math.sqrt(2.0 * rho * area_m2)


def hover_power_w(mass_kg, rho=RHO_SEA, area_m2=None):
    """Real hover power = ideal momentum power / figure of merit."""
    ideal = ideal_hover_power_w(hover_thrust_n(mass_kg), rho, area_m2)
    return ideal / VEHICLE["figure_of_merit"]


def hover_power_for_payload(payload_kg):
    return hover_power_w(mtom_kg(payload_kg))


def endurance_min(payload_kg, soc_frac=1.0):
    """Hover endurance on the usable pack at a given state of charge."""
    usable_kwh = VEHICLE["battery_kwh"] * VEHICLE["usable_soc_frac"] * soc_frac
    return usable_kwh * 1000.0 / hover_power_for_payload(payload_kg) * 60.0


def payload_class_table():
    """The three payload classes with their headline lift numbers."""
    rows = []
    for p in PAYLOAD_CLASSES:
        mass = mtom_kg(p)
        rows.append({
            "payload_kg": p,
            "mtom_kg": mass,
            "hover_thrust_n": round(hover_thrust_n(mass), 1),
            "per_motor_n": round(thrust_per_motor_n(mass), 1),
            "disk_loading_n_m2": round(disk_loading_n_m2(mass), 1),
            "hover_power_kw": round(hover_power_for_payload(p) / 1000.0, 2),
            "endurance_min": round(endurance_min(p), 1),
            "thrust_margin": round(thrust_margin(mass), 2),
        })
    return rows


# ---------------------------------------------------------------------------
# 2. Slung-load dynamics — pendulum on a cable under the vehicle.
#    Linearised pendulum:  th'' = -(g/L) th - a(t)/L - 2 zeta w th'
#    Natural frequency f = sqrt(g/L) / (2 pi) — set by cable length only.
#    Two cures are modelled and compared honestly:
#      * Input shaping (ZV): split the accel command into two half-steps
#        one half-period apart; the second cancels the first's swing.
#      * PD sway feedback: the vehicle leans against the sway rate.
#    Rigid mount is the third option: no pendulum at all, but the load's
#    lateral inertia lands straight on the airframe (load factor a/g).
# ---------------------------------------------------------------------------
CABLE_LENGTH_M = 3.0


def pendulum_frequency_hz(cable_m=CABLE_LENGTH_M):
    return math.sqrt(G / cable_m) / (2.0 * math.pi)


def pendulum_period_s(cable_m=CABLE_LENGTH_M):
    return 1.0 / pendulum_frequency_hz(cable_m)


def _accel_profile(t, a0, duration):
    """Rectangular accel command: a0 for `duration` seconds, then 0."""
    return a0 if 0.0 <= t < duration else 0.0


def swing_sim(a0=1.2, duration=4.0, cable_m=CABLE_LENGTH_M, shaped=False,
              pd_gain=0.0, t_end=30.0, dt=0.001):
    """Simulate slung-load swing angle (deg) for one accel pulse.

    shaped=True applies a ZV input shaper to the accel command.
    pd_gain>0 adds vehicle accel feedback proportional to sway rate.
    Returns (max_swing_deg, residual_swing_deg) where the residual is
    the max |angle| in the last 5 s, after the move is long over.
    """
    omega = math.sqrt(G / cable_m)
    zeta = 0.01                      # bare pendulum damping (labelled)
    half_period = 0.5 * pendulum_period_s(cable_m)

    def command(t):
        if not shaped:
            return _accel_profile(t, a0, duration)
        # ZV shaper for a near-undamped pendulum: two half impulses
        return (0.5 * _accel_profile(t, a0, duration)
                + 0.5 * _accel_profile(t - half_period, a0, duration))

    th, thd = 0.0, 0.0
    max_swing = 0.0
    residual = 0.0
    steps = int(t_end / dt)
    for i in range(steps + 1):
        t = i * dt
        a_cmd = command(t) + pd_gain * thd * cable_m
        acc = -omega * omega * th - (a_cmd / cable_m) - 2.0 * zeta * omega * thd
        thd += acc * dt
        th += thd * dt
        deg = abs(math.degrees(th))
        max_swing = max(max_swing, deg)
        if t >= t_end - 5.0:
            residual = max(residual, deg)
    return round(max_swing, 3), round(residual, 3)


def rigid_mount_load_factor(accel_mps2):
    """Rigid mount: no swing, but lateral load factor = a / g."""
    return accel_mps2 / G


def rigid_mount_cg_shift_m(payload_kg, mount_offset_m=0.30):
    """Rigid mount: the load's CG offset is carried by the whole vehicle."""
    return mount_offset_m * payload_kg / mtom_kg(payload_kg)


# ---------------------------------------------------------------------------
# 3. Energy / range — cruise at a fixed speed, hover segments at both
#    ends, and a hard planning rule: never land below RESERVE_SOC.
# ---------------------------------------------------------------------------
CRUISE_SPEED_MPS = 15.0
CRUISE_POWER_FRAC = 0.85     # cruise draws 85% of hover power (sim model)
HOVER_SEGMENTS_MIN = 2.0     # pick-up hover + drop-off hover


def trip_energy_kwh(payload_kg, distance_km):
    """One-way trip energy: cruise the distance + hover at both ends."""
    p_hover = hover_power_for_payload(payload_kg)
    p_cruise = p_hover * CRUISE_POWER_FRAC
    cruise_min = distance_km * 1000.0 / CRUISE_SPEED_MPS / 60.0
    energy = (p_cruise * cruise_min + p_hover * HOVER_SEGMENTS_MIN) / 60.0 / 1000.0
    return energy, cruise_min + HOVER_SEGMENTS_MIN


def plan_trip(payload_kg, distance_km, soc_start=0.95):
    """Plan one delivery leg. REFUSED if it would breach the reserve."""
    energy_kwh, total_min = trip_energy_kwh(payload_kg, distance_km)
    soc_used = energy_kwh / VEHICLE["battery_kwh"]
    soc_end = soc_start - soc_used
    ok = soc_end >= RESERVE_SOC
    return {
        "payload_kg": payload_kg,
        "distance_km": distance_km,
        "energy_kwh": round(energy_kwh, 3),
        "total_min": round(total_min, 2),
        "soc_start": soc_start,
        "soc_end": round(soc_end, 4),
        "reserve_ok": ok,
        "verdict": "PLANNED" if ok else "REFUSED",
        "reason": ("lands at SoC "
                   f"{soc_end:.0%}, above the {RESERVE_SOC:.0%} reserve"
                   if ok else
                   f"would land at SoC {soc_end:.0%}, below the "
                   f"{RESERVE_SOC:.0%} reserve — never planned"),
    }


def max_range_km(payload_kg, soc_start=0.95):
    """Longest one-way leg that still lands exactly at the reserve."""
    budget_kwh = (soc_start - RESERVE_SOC) * VEHICLE["battery_kwh"]
    p_hover = hover_power_for_payload(payload_kg)
    hover_kwh = p_hover * HOVER_SEGMENTS_MIN / 60.0 / 1000.0
    cruise_kwh = budget_kwh - hover_kwh
    cruise_min = cruise_kwh * 1000.0 * 60.0 / (p_hover * CRUISE_POWER_FRAC)
    return round(cruise_min * 60.0 * CRUISE_SPEED_MPS / 1000.0, 1)


def range_table(soc_start=0.95):
    return [{"payload_kg": p, "max_range_km": max_range_km(p, soc_start)}
            for p in PAYLOAD_CLASSES]


# Porter Ranch pad -> LA warehouse district (sim straight-line distance)
WAREHOUSE_HOP = {"from": "Porter Ranch pad", "to": "LA warehouse district",
                 "dist_km": 38.0, "payload_kg": 50.0}


# ---------------------------------------------------------------------------
# 4. Wind limits — a hanging container is a sail. Steady wind swings the
#    load until drag balances gravity: tan(theta) = F_drag / (m_load g).
#    The lab caps steady swing at 10 deg; heavier loads carry more
#    restoring weight per sail area, so their wind limit is HIGHER —
#    until the vehicle's own structural/governor cap takes over.
# ---------------------------------------------------------------------------
SAIL_AREA_M2 = {10.0: 0.6, 50.0: 1.2, 200.0: 2.4}   # container face, sim
DRAG_CD = 1.05
MAX_SWING_DEG = 10.0
VEHICLE_WIND_CAP_MPS = 12.0    # airframe / governor cap, all classes


def sail_area_m2(payload_kg):
    """Container face area; non-class weights use the nearest class at
    or below them (overweight loads keep the heavy-class sail area)."""
    if payload_kg in SAIL_AREA_M2:
        return SAIL_AREA_M2[payload_kg]
    below = [c for c in PAYLOAD_CLASSES if c <= payload_kg]
    return SAIL_AREA_M2[max(below)] if below else SAIL_AREA_M2[min(PAYLOAD_CLASSES)]


def wind_drag_n(wind_mps, payload_kg):
    area = sail_area_m2(payload_kg)
    return 0.5 * RHO_SEA * wind_mps ** 2 * area * DRAG_CD


def steady_swing_deg(wind_mps, payload_kg):
    f = wind_drag_n(wind_mps, payload_kg)
    return math.degrees(math.atan(f / (payload_kg * G)))


def max_wind_mps(payload_kg):
    """Wind at which steady swing hits the cap, vehicle cap applied."""
    f_max = payload_kg * G * math.tan(math.radians(MAX_SWING_DEG))
    area = sail_area_m2(payload_kg)
    v = math.sqrt(2.0 * f_max / (RHO_SEA * area * DRAG_CD))
    return round(min(v, VEHICLE_WIND_CAP_MPS), 2)


def wind_verdict(payload_kg, forecast_mps):
    limit = max_wind_mps(payload_kg)
    if forecast_mps > limit:
        return "NO-GO"
    if forecast_mps > limit - 2.0:
        return "CAUTION"
    return "GO"


# ---------------------------------------------------------------------------
# 5. Governor + load interlocks — X-family style. The governor clamps
#    speed/altitude by zone; the interlocks are pass/fail and ALL must
#    pass before the (simulated) departure. Overload is a REFUSAL, not
#    a warning.
# ---------------------------------------------------------------------------
ABSOLUTE_CEILING_M = 120.0
ZONES = {
    "PAD":        {"speed_mps": 3.0,  "alt_m": 15.0},
    "YARD":       {"speed_mps": 8.0,  "alt_m": 40.0},
    "CORRIDOR":   {"speed_mps": 15.0, "alt_m": 90.0},
    "TEST_AREA":  {"speed_mps": 20.0, "alt_m": 120.0},
}


def governor_clamp(zone, speed_mps, alt_m):
    z = ZONES[zone]
    alt_cap = min(z["alt_m"], ABSOLUTE_CEILING_M)
    return {
        "zone": zone,
        "speed_mps": min(speed_mps, z["speed_mps"]),
        "alt_m": min(alt_m, alt_cap),
        "speed_clamped": speed_mps > z["speed_mps"],
        "alt_clamped": alt_m > alt_cap,
    }


CG_BOX_M = 0.25          # load CG must sit inside +/- this box
PICKUP_SWAY_MAX_DEG = 2.0


def load_interlocks(state):
    """Every check must pass. Returns per-check detail + verdict."""
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    add("weight_within_limit",
        state["measured_kg"] <= VEHICLE["max_payload_kg"],
        f"measured {state['measured_kg']:.0f} kg vs "
        f"{VEHICLE['max_payload_kg']:.0f} kg max payload")
    add("weight_matches_declaration",
        abs(state["measured_kg"] - state["declared_kg"]) <= 0.05 * state["declared_kg"],
        f"measured {state['measured_kg']:.0f} kg vs declared "
        f"{state['declared_kg']:.0f} kg (5% tolerance)")
    add("cg_within_box",
        abs(state["cg_offset_m"]) <= CG_BOX_M,
        f"CG offset {state['cg_offset_m']:.2f} m inside +/-{CG_BOX_M:.2f} m box")
    add("latch_confirmed", state["latch_confirmed"],
        "cargo latch / sling attachment confirmed")
    add("sway_below_threshold",
        state["sway_deg"] < PICKUP_SWAY_MAX_DEG,
        f"pickup sway {state['sway_deg']:.1f} deg < {PICKUP_SWAY_MAX_DEG:.0f} deg")
    add("battery_ready", state["soc"] >= 0.60,
        f"SoC {state['soc']:.0%} >= 60% departure floor")
    add("wind_within_class_limit",
        state["wind_mps"] <= max_wind_mps(state["declared_kg"]),
        f"wind {state['wind_mps']:.1f} m/s vs class limit "
        f"{max_wind_mps(state['declared_kg']):.1f} m/s")
    add("motors_all_ok", state["motors_ok"] == VEHICLE["motors"],
        f"{state['motors_ok']}/{VEHICLE['motors']} motors reporting OK")
    add("governor_zone_set", state["governor_zone_set"],
        "governor zone + caps loaded")
    add("spotter_confirmed", state["spotter_confirmed"],
        "human spotter has eyes on the load")

    all_pass = all(c["ok"] for c in checks)
    return {"checks": checks, "all_pass": all_pass,
            "verdict": "CLEARED" if all_pass else "REFUSED"}


NOMINAL_LOAD_STATE = {
    "measured_kg": 50.0, "declared_kg": 50.0, "cg_offset_m": 0.08,
    "latch_confirmed": True, "sway_deg": 0.8, "soc": 0.92,
    "wind_mps": 5.0, "motors_ok": 6, "governor_zone_set": True,
    "spotter_confirmed": True,
}


# ---------------------------------------------------------------------------
# 6. Motor-out — a hex that loses a motor keeps most of its thrust but
#    loses control margin. The verdict follows the recomputed margin:
#      margin >= 2.00  -> CONTINUE (sim, with the failure logged)
#      margin >= 1.60  -> LAND NOW: controlled descent, no new cargo leg
#      margin <  1.60  -> EMERGENCY DESCENT at the spot below
# ---------------------------------------------------------------------------
def motor_out_check(payload_kg, motors_out=1):
    mass = mtom_kg(payload_kg)
    up = VEHICLE["motors"] - motors_out
    margin = thrust_margin(mass, motors_up=up)
    if margin >= 2.00:
        verdict = "CONTINUE"
    elif margin >= 1.60:
        verdict = "LAND_NOW"
    else:
        verdict = "EMERGENCY_DESCENT"
    return {
        "payload_kg": payload_kg,
        "motors_out": motors_out,
        "motors_up": up,
        "available_n": round(max_total_thrust_n(up), 1),
        "needed_n": round(hover_thrust_n(mass), 1),
        "margin": round(margin, 2),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# 7. Fleet note — three vehicles share ONE pad. Pad occupancy is a fixed
#    turnaround block; requests are scheduled deterministically by
#    (ready time, vehicle id) so the queue never double-books the pad.
# ---------------------------------------------------------------------------
PAD_TURNAROUND_MIN = 6.0


def schedule_fleet(requests):
    """requests: [{vehicle, ready_min}] -> pad slots with no overlaps."""
    ordered = sorted(requests, key=lambda r: (r["ready_min"], r["vehicle"]))
    slots = []
    pad_free = 0.0
    for r in ordered:
        start = max(r["ready_min"], pad_free)
        end = start + PAD_TURNAROUND_MIN
        slots.append({"vehicle": r["vehicle"], "start_min": round(start, 1),
                      "end_min": round(end, 1)})
        pad_free = end
    return slots


def slots_conflict(slots):
    s = sorted(slots, key=lambda x: x["start_min"])
    return any(b["start_min"] < a["end_min"] for a, b in zip(s, s[1:]))


FLEET_REQUESTS = [
    {"vehicle": "HCX-2", "ready_min": 4.0},
    {"vehicle": "HCX-1", "ready_min": 0.0},
    {"vehicle": "HCX-3", "ready_min": 9.0},
]


# ---------------------------------------------------------------------------
# 8. Regime classifier — stdlib kNN on (payload_frac, wind, distance, soc)
# ---------------------------------------------------------------------------
KNN_TRAINING = [
    # payload_frac, wind_mps, distance_km, soc, label
    (0.05, 3.0, 10.0, 0.95, "go"), (0.25, 5.0, 20.0, 0.90, "go"),
    (0.10, 4.0, 38.0, 0.92, "go"), (0.50, 6.0, 15.0, 0.85, "go"),
    (0.25, 2.0, 5.0, 0.80, "go"), (0.40, 7.0, 25.0, 0.88, "go"),
    (0.60, 8.0, 30.0, 0.70, "caution"), (0.75, 6.0, 20.0, 0.65, "caution"),
    (0.50, 10.0, 35.0, 0.75, "caution"), (0.90, 5.0, 12.0, 0.60, "caution"),
    (0.30, 9.0, 45.0, 0.70, "caution"), (0.65, 9.0, 10.0, 0.55, "caution"),
    (1.00, 12.0, 40.0, 0.50, "no-go"), (0.90, 13.0, 25.0, 0.45, "no-go"),
    (0.80, 11.0, 55.0, 0.60, "no-go"), (1.00, 8.0, 60.0, 0.40, "no-go"),
    (0.60, 14.0, 30.0, 0.50, "no-go"), (0.95, 10.0, 50.0, 0.35, "no-go"),
]


def _knn_scale(row):
    return (row[0], row[1] / 15.0, row[2] / 60.0, row[3])


def knn_classify(payload_frac, wind_mps, distance_km, soc, k=3):
    q = _knn_scale((payload_frac, wind_mps, distance_km, soc))
    dists = []
    for row in KNN_TRAINING:
        p = _knn_scale(row)
        d = math.sqrt(sum((a - b) ** 2 for a, b in zip(q, p)))
        dists.append((d, row[4]))
    dists.sort(key=lambda x: x[0])
    votes = {}
    for _, label in dists[:k]:
        votes[label] = votes.get(label, 0) + 1
    return max(sorted(votes), key=lambda lab: votes[lab])


def knn_training_accuracy():
    right = 0
    for row in KNN_TRAINING:
        if knn_classify(row[0], row[1], row[2], row[3]) == row[4]:
            right += 1
    return right / len(KNN_TRAINING)


# ---------------------------------------------------------------------------
# 9. Advisory seats — compact, same pattern as the other X labs
# ---------------------------------------------------------------------------
def carbon4_decide(scenario):
    """Gemini Carbon 4 DECIDER seat. Names-only (no key is read here),
    tighten-only: it can turn PROCEED into HOLD, never the reverse.
    Pipeline MAP -> MODEL -> STRESS -> DECIDE, sha256 trace."""
    summary = "|".join(f"{k}={scenario[k]}" for k in sorted(scenario))
    trace = hashlib.sha256(("carbon4:" + summary).encode()).hexdigest()
    verdict = "HOLD" if scenario.get("live_action") else "PROCEED"
    if scenario.get("interlocks_pass") is False:
        verdict = "HOLD"
    if scenario.get("refused") is True:
        verdict = "HOLD"
    return {
        "seat": "gemini-carbon-4-extended-pro-thinking",
        "role": "DECIDER",
        "stages": ["MAP", "MODEL", "STRESS", "DECIDE"],
        "tiers": [2048, 8192, 32768],
        "verdict": verdict,
        "tighten_only": True,
        "trace_sha256": trace,
    }


MUSE_ADVISOR = {
    "seat": "muse-spark",
    "role": "ADVISOR",
    "can_override_hold": False,
    "authority": "none — advice only, a HOLD from any seat is final",
}


def flow_plan():
    """Google Flow seat: plan-only storyboard. Never rendered live."""
    return {
        "seat": "google-flow",
        "mode": "plan-only",
        "rendered": False,
        "live": 0,
        "shots": [
            "S1 warehouse pad at dusk, container staged under the hex",
            "S2 interlock board: weight, CG, latch all green",
            "S3 rotors spool, sling goes taut, sway settles",
            "S4 corridor run, load riding quiet under the vehicle",
            "S5 drop-off hover, release, empty climb away",
        ],
    }


# ---------------------------------------------------------------------------
# 10. HOLD gates — live actions that are NEVER done by this lab
# ---------------------------------------------------------------------------
HOLD_GATES = {
    "live_flight": "No real flight — simulation only.",
    "cargo_ops_live": "No real cargo is picked up, carried, or dropped.",
    "real_hardware_control": "No motors, ESCs, winches, or latches are driven.",
    "bypass_governor": "Governor caps cannot be bypassed.",
    "disable_load_interlocks": "Load interlocks cannot be disabled or skipped.",
    "fly_over_people_live": "No live flight over people or property.",
    "hazmat_cargo_live": "No hazardous cargo is ever carried live.",
    "claim_certified": "Never claim this sim is a certified cargo aircraft.",
    "commercial_service_claim": "No delivery service is offered or implied.",
    "remove_human_override": "Human override is never removed.",
    "bvlos_live": "No live beyond-visual-line-of-sight operation.",
    "exceed_payload_live": "The payload limit cannot be exceeded live.",
    "medical_delivery_claim": "No medical delivery capability is claimed.",
    "investment_claim": "No investment or revenue claim is made.",
}


def hold_demo(action="live_flight"):
    gate = HOLD_GATES.get(action, "unknown live action")
    print(f"HOLD: {action} — {gate}")
    print("VERDICT: HOLD (exit 2)")
    return 2


# ---------------------------------------------------------------------------
# CHECKS — every number below is asserted against an analytic value
# ---------------------------------------------------------------------------
def run_checks():
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    # --- lift model ---
    add("hover_thrust_is_mg",
        abs(hover_thrust_n(320.0) - 320.0 * G) < 1e-6,
        f"T = {hover_thrust_n(320.0):.1f} N = m*g at max MTOW 320 kg")
    add("thrust_per_motor_sums",
        abs(thrust_per_motor_n(320.0) * 6 - hover_thrust_n(320.0)) < 1e-6,
        f"{thrust_per_motor_n(320.0):.1f} N x 6 motors")
    add("disk_area",
        abs(disk_area_m2() - 6 * math.pi * 0.90 ** 2) < 1e-9,
        f"A = {disk_area_m2():.3f} m^2 from 6 rotors of R = 0.90 m")
    t_ref, a_ref = 4000.0, 15.279
    expected = t_ref ** 1.5 / math.sqrt(2 * RHO_SEA * a_ref)
    add("momentum_power_analytic",
        abs(ideal_hover_power_w(t_ref, RHO_SEA, a_ref) - expected) < 1e-6,
        f"P_ideal(4000 N) = {expected:.0f} W")
    p1 = ideal_hover_power_w(2000.0, RHO_SEA, a_ref)
    p2 = ideal_hover_power_w(4000.0, RHO_SEA, a_ref)
    add("power_scales_T_3_2",
        abs(p2 / p1 - 2 ** 1.5) < 1e-9,
        f"doubling T scales P by {p2 / p1:.4f} = 2^1.5")
    p_small = ideal_hover_power_w(t_ref, RHO_SEA, a_ref / 4.0)
    add("power_scales_inv_sqrt_A",
        abs(p_small / p2 - 2.0) < 1e-9,
        f"quarter disk area doubles P (x{p_small / p2:.3f}) — big rotors win")
    add("hover_power_uses_fom",
        abs(hover_power_w(320.0)
            - ideal_hover_power_w(hover_thrust_n(320.0)) / 0.75) < 1e-6,
        f"P_hover = {hover_power_w(320.0) / 1000:.1f} kW at 320 kg")
    table = {r["payload_kg"]: r for r in payload_class_table()}
    add("heavier_class_more_power",
        table[10.0]["hover_power_kw"] < table[50.0]["hover_power_kw"]
        < table[200.0]["hover_power_kw"],
        "hover power rises across the 10 / 50 / 200 kg classes: "
        f"{table[10.0]['hover_power_kw']} < {table[50.0]['hover_power_kw']} "
        f"< {table[200.0]['hover_power_kw']} kW")
    add("heavier_class_less_endurance",
        table[10.0]["endurance_min"] > table[50.0]["endurance_min"]
        > table[200.0]["endurance_min"],
        "endurance falls as payload grows")
    add("all_classes_positive_margin",
        all(r["thrust_margin"] > 1.5 for r in table.values()),
        "every class hovers with thrust margin > 1.5 on all 6 motors")

    # --- slung-load dynamics ---
    add("pendulum_frequency_analytic",
        abs(pendulum_frequency_hz(3.0)
            - math.sqrt(G / 3.0) / (2 * math.pi)) < 1e-12,
        f"f = {pendulum_frequency_hz(3.0):.3f} Hz on a 3 m cable")
    add("pendulum_freq_falls_with_length",
        pendulum_frequency_hz(6.0) < pendulum_frequency_hz(3.0)
        < pendulum_frequency_hz(1.5),
        "longer cable, slower swing — frequency set by length alone")
    max_raw, res_raw = swing_sim(shaped=False)
    max_shp, res_shp = swing_sim(shaped=True)
    add("unshaped_swing_is_real", res_raw > 2.0,
        f"unshaped accel pulse leaves {res_raw:.2f} deg residual swing")
    add("input_shaping_cancels_swing",
        res_shp < 0.25 * res_raw,
        f"ZV-shaped residual {res_shp:.3f} deg < 25% of unshaped {res_raw:.2f}")
    max_pd, res_pd = swing_sim(shaped=False, pd_gain=0.9)
    add("pd_feedback_damps_swing",
        res_pd < res_raw and max_pd < max_raw,
        f"PD sway feedback: max {max_pd:.2f} < {max_raw:.2f} deg, "
        f"residual {res_pd:.3f} < {res_raw:.2f} deg")
    add("rigid_mount_load_factor",
        abs(rigid_mount_load_factor(G) - 1.0) < 1e-12
        and abs(rigid_mount_load_factor(1.2) - 1.2 / G) < 1e-12,
        "rigid mount trades swing for a straight a/g load factor")
    add("rigid_mount_cg_shift",
        abs(rigid_mount_cg_shift_m(200.0) - 0.30 * 200.0 / 320.0) < 1e-9,
        f"200 kg rigid load shifts CG {rigid_mount_cg_shift_m(200.0):.3f} m")

    # --- energy / range ---
    hop = plan_trip(WAREHOUSE_HOP["payload_kg"], WAREHOUSE_HOP["dist_km"])
    add("warehouse_hop_planned", hop["verdict"] == "PLANNED",
        f"Porter Ranch -> LA warehouse, 38 km at 50 kg: {hop['energy_kwh']} "
        f"kWh, SoC {hop['soc_start']:.0%} -> {hop['soc_end']:.0%}")
    add("hop_respects_reserve", hop["soc_end"] >= RESERVE_SOC,
        f"hop lands at {hop['soc_end']:.0%}, reserve is {RESERVE_SOC:.0%}")
    e_chk, _ = trip_energy_kwh(50.0, 38.0)
    add("trip_energy_audit",
        abs(e_chk - hop["energy_kwh"] / 1.0) < 0.01
        and abs(hop["soc_end"] - (0.95 - e_chk / 30.0)) < 0.01,
        "energy audit: SoC drop equals audited energy / pack size")
    far = plan_trip(200.0, 120.0)
    add("overrange_refused", far["verdict"] == "REFUSED",
        f"200 kg over 120 km would breach the reserve -> {far['verdict']}")
    rt = {r["payload_kg"]: r["max_range_km"] for r in range_table()}
    add("range_falls_with_payload",
        rt[10.0] > rt[50.0] > rt[200.0],
        f"max range {rt[10.0]} > {rt[50.0]} > {rt[200.0]} km by class")
    add("range_lands_on_reserve",
        abs(plan_trip(50.0, rt[50.0])["soc_end"] - RESERVE_SOC) < 0.01,
        "a max-range leg lands within 1% of the reserve floor")

    # --- wind limits ---
    add("wind_limits_rise_with_class",
        max_wind_mps(10.0) < max_wind_mps(50.0) < max_wind_mps(200.0),
        f"limits {max_wind_mps(10.0)} < {max_wind_mps(50.0)} < "
        f"{max_wind_mps(200.0)} m/s (heavier load, more restoring weight)")
    add("wind_limit_capped_by_vehicle",
        max_wind_mps(200.0) == VEHICLE_WIND_CAP_MPS,
        f"heavy class hits the {VEHICLE_WIND_CAP_MPS:.0f} m/s vehicle cap")
    add("swing_at_limit_is_cap",
        abs(steady_swing_deg(max_wind_mps(50.0), 50.0) - MAX_SWING_DEG) < 0.1,
        "at the class wind limit the steady swing is the 10 deg cap")
    add("wind_verdicts",
        wind_verdict(50.0, 5.0) == "GO"
        and wind_verdict(10.0, max_wind_mps(10.0) + 1.0) == "NO-GO",
        "calm medium load GO; over-limit light load NO-GO")

    # --- governor + interlocks ---
    gc = governor_clamp("YARD", 20.0, 80.0)
    add("governor_clamps_yard",
        gc["speed_mps"] == 8.0 and gc["alt_m"] == 40.0 and gc["speed_clamped"],
        "20 m/s / 80 m request clamped to YARD caps 8 m/s / 40 m")
    gc2 = governor_clamp("TEST_AREA", 99.0, 999.0)
    add("governor_absolute_ceiling", gc2["alt_m"] == ABSOLUTE_CEILING_M,
        f"altitude hard-capped at {ABSOLUTE_CEILING_M:.0f} m")
    add("interlocks_nominal_cleared",
        load_interlocks(NOMINAL_LOAD_STATE)["verdict"] == "CLEARED",
        "nominal load state: all 10 interlocks pass")
    over = dict(NOMINAL_LOAD_STATE, measured_kg=240.0, declared_kg=240.0)
    add("overload_refused",
        load_interlocks(over)["verdict"] == "REFUSED",
        "240 kg measured vs 200 kg limit -> REFUSED, not a warning")
    offcg = dict(NOMINAL_LOAD_STATE, cg_offset_m=0.40)
    add("cg_out_of_box_refused",
        load_interlocks(offcg)["verdict"] == "REFUSED",
        "CG 0.40 m outside the +/-0.25 m box -> REFUSED")
    unlatched = dict(NOMINAL_LOAD_STATE, latch_confirmed=False)
    add("unlatch_refused",
        load_interlocks(unlatched)["verdict"] == "REFUSED",
        "latch unconfirmed -> REFUSED")

    # --- motor-out ---
    mo_light = motor_out_check(50.0, motors_out=1)
    add("motor_out_light_continues", mo_light["verdict"] == "CONTINUE",
        f"50 kg, 1 motor out: margin {mo_light['margin']} -> CONTINUE")
    mo_heavy = motor_out_check(200.0, motors_out=1)
    add("motor_out_heavy_lands_now", mo_heavy["verdict"] == "LAND_NOW",
        f"200 kg, 1 motor out: margin {mo_heavy['margin']} -> LAND_NOW "
        "(controlled descent, no new cargo leg)")
    mo_two = motor_out_check(200.0, motors_out=2)
    add("motor_out_two_heavy_emergency",
        mo_two["verdict"] == "EMERGENCY_DESCENT",
        f"200 kg, 2 motors out: margin {mo_two['margin']} -> "
        "EMERGENCY_DESCENT")
    add("motor_out_margin_math",
        abs(mo_heavy["margin"]
            - 5 * 1200.0 / hover_thrust_n(320.0)) < 0.01,
        "margin = remaining max thrust / hover thrust, recomputed honestly")

    # --- fleet scheduling ---
    slots = schedule_fleet(FLEET_REQUESTS)
    add("fleet_no_pad_conflicts", not slots_conflict(slots),
        "3 vehicles, 1 pad: no overlapping pad slots")
    add("fleet_order_deterministic",
        [s["vehicle"] for s in slots] == ["HCX-1", "HCX-2", "HCX-3"],
        "queue ordered by (ready time, vehicle id)")
    add("fleet_slots_are_turnaround_blocks",
        all(abs(s["end_min"] - s["start_min"] - PAD_TURNAROUND_MIN) < 1e-9
            for s in slots),
        f"every pad slot is one {PAD_TURNAROUND_MIN:.0f} min turnaround")

    # --- classifier ---
    add("knn_go_case", knn_classify(0.25, 4.0, 20.0, 0.90) == "go",
        "medium / calm / near / charged -> go")
    add("knn_nogo_case", knn_classify(1.0, 13.0, 50.0, 0.40) == "no-go",
        "max / windy / far / low -> no-go")
    acc = knn_training_accuracy()
    add("knn_training_accuracy", acc >= 0.85,
        f"kNN self-accuracy {acc:.3f} on its training set")

    # --- seats ---
    c_ok = carbon4_decide({"live_action": False, "interlocks_pass": True})
    c_hold = carbon4_decide({"live_action": True, "interlocks_pass": True})
    add("carbon4_trace_deterministic",
        len(c_ok["trace_sha256"]) == 64
        and carbon4_decide({"live_action": False, "interlocks_pass": True})
        ["trace_sha256"] == c_ok["trace_sha256"],
        "sha256 trace is 64 hex chars and repeatable")
    add("carbon4_tightens_to_hold",
        c_ok["verdict"] == "PROCEED" and c_hold["verdict"] == "HOLD",
        "sim PROCEED, live action HOLD")
    add("muse_never_overrides", MUSE_ADVISOR["can_override_hold"] is False,
        "Muse advisor can_override_hold=False")
    fp = flow_plan()
    add("flow_plan_only", fp["rendered"] is False and fp["live"] == 0,
        "Flow rendered=False, live=0")

    # --- HOLD gates ---
    add("hold_gates_count", len(HOLD_GATES) == 14,
        f"{len(HOLD_GATES)} HOLD gates defined")
    add("hold_gates_cover_cargo_risks",
        "hazmat_cargo_live" in HOLD_GATES
        and "disable_load_interlocks" in HOLD_GATES
        and "exceed_payload_live" in HOLD_GATES,
        "hazmat, interlock-disable, and payload-exceed all held")

    passed = sum(1 for c in checks if c["ok"])
    return checks, passed


def print_checks():
    checks, passed = run_checks()
    print("-" * 68)
    print("CHECKS")
    print("-" * 68)
    for c in checks:
        print(f"  [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    verdict = "PROCEED" if passed == len(checks) else "HOLD"
    print(f"\nCHECKS: {passed}/{len(checks)}")
    print(f"VERDICT: {verdict} (sandbox simulation only) | HOLD gates "
          f"{len(HOLD_GATES)}")
    return 0 if verdict == "PROCEED" else 2


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
def demo():
    print("=" * 68)
    print("HoverCargo-X — Heavy-Lift Hover Cargo Transport Lab (SIM ONLY)")
    print("by Darion Kellar")
    print("=" * 68)

    print(f"\n[1] Vehicle: {VEHICLE['name']} — {VEHICLE['motors']} rotors, "
          f"R = {VEHICLE['rotor_radius_m']} m, disk {disk_area_m2():.2f} m^2")
    print("    Payload classes (momentum theory, FoM "
          f"{VEHICLE['figure_of_merit']}):")
    for r in payload_class_table():
        print(f"    {r['payload_kg']:>5.0f} kg payload -> MTOW {r['mtom_kg']:.0f} kg, "
              f"hover {r['hover_power_kw']:.1f} kW, "
              f"endurance {r['endurance_min']:.0f} min, "
              f"margin x{r['thrust_margin']}")

    p_a = ideal_hover_power_w(4000.0, RHO_SEA, disk_area_m2())
    p_b = ideal_hover_power_w(4000.0, RHO_SEA, disk_area_m2() / 4.0)
    print(f"\n[2] Disk loading trade-off at 4000 N: full disk {p_a:.0f} W "
          f"ideal vs quarter disk {p_b:.0f} W — small rotors pay double. "
          "Big and slow wins.")

    max_raw, res_raw = swing_sim(shaped=False)
    max_shp, res_shp = swing_sim(shaped=True)
    max_pd, res_pd = swing_sim(shaped=False, pd_gain=0.9)
    print(f"\n[3] Slung load on a {CABLE_LENGTH_M:.0f} m cable "
          f"(pendulum {pendulum_frequency_hz():.3f} Hz):")
    print(f"    unshaped accel pulse: max {max_raw:.2f} deg, "
          f"residual {res_raw:.2f} deg")
    print(f"    ZV input shaping:     max {max_shp:.2f} deg, "
          f"residual {res_shp:.3f} deg")
    print(f"    PD sway feedback:     max {max_pd:.2f} deg, "
          f"residual {res_pd:.3f} deg")
    print(f"    rigid mount instead: no swing, load factor "
          f"{rigid_mount_load_factor(1.2):.3f} at 1.2 m/s^2 accel")

    print("\n[4] Range table (one-way, lands at the 25% reserve):")
    for r in range_table():
        print(f"    {r['payload_kg']:>5.0f} kg -> {r['max_range_km']} km")
    hop = plan_trip(WAREHOUSE_HOP["payload_kg"], WAREHOUSE_HOP["dist_km"])
    print(f"    Warehouse hop: {hop['verdict']} — {hop['reason']}")

    print("\n[5] Wind limits by class (10 deg steady-swing cap):")
    for p in PAYLOAD_CLASSES:
        print(f"    {p:>5.0f} kg: max {max_wind_mps(p):.1f} m/s, "
              f"forecast 5 m/s -> {wind_verdict(p, 5.0)}")

    il = load_interlocks(NOMINAL_LOAD_STATE)
    print(f"\n[6] Load interlocks (nominal 50 kg load): {il['verdict']}")
    for c in il["checks"]:
        print(f"    [{'PASS' if c['ok'] else 'FAIL'}] {c['name']}: {c['detail']}")
    over = load_interlocks(dict(NOMINAL_LOAD_STATE, measured_kg=240.0,
                                declared_kg=240.0))
    print(f"    Overload case (240 kg): {over['verdict']}")

    print("\n[7] Motor-out verdicts (hex):")
    for p, out in ((50.0, 1), (200.0, 1), (200.0, 2)):
        mo = motor_out_check(p, motors_out=out)
        print(f"    {p:.0f} kg, {out} out: margin x{mo['margin']} "
              f"-> {mo['verdict']}")

    slots = schedule_fleet(FLEET_REQUESTS)
    print("\n[8] Fleet pad schedule (3 vehicles, 1 pad):")
    for s in slots:
        print(f"    {s['vehicle']}: pad {s['start_min']:.0f}-"
              f"{s['end_min']:.0f} min")

    print(f"\n[9] Regime classifier: medium/calm/near/charged = "
          f"{knn_classify(0.25, 4.0, 20.0, 0.90)}, max/windy/far/low = "
          f"{knn_classify(1.0, 13.0, 50.0, 0.40)}")

    dec = carbon4_decide({"live_action": False, "interlocks_pass": True})
    print(f"\n[10] Carbon 4 decider: {dec['verdict']} "
          f"(trace {dec['trace_sha256'][:16]}...)")
    print(f"     Muse advisor: can_override_hold="
          f"{MUSE_ADVISOR['can_override_hold']}")
    fp = flow_plan()
    print(f"     Flow: rendered={fp['rendered']}, live={fp['live']}, "
          f"{len(fp['shots'])} shots planned")
    print(f"\n[11] HOLD gates: {len(HOLD_GATES)} — live flight, cargo ops, "
          "hazmat, interlock disable, payload exceed... all HOLD.")
    print()
    return print_checks()


def main(argv):
    if len(argv) > 1:
        cmd = argv[1]
        if cmd == "hold-demo":
            action = argv[2] if len(argv) > 2 else "live_flight"
            return hold_demo(action)
        if cmd == "holds":
            for name, desc in HOLD_GATES.items():
                print(f"HOLD {name}: {desc}")
            return 0
        if cmd == "checks":
            return print_checks()
        if cmd == "demo":
            return demo()
        print(f"unknown command: {cmd}")
        return 2
    return demo()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
