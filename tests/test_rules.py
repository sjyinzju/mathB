from __future__ import annotations

import pytest

from src.rules import (
    advance_fuel,
    can_take_leg,
    flight_minutes,
    fuel_after_leg,
    minimum_stop_minutes,
    refuel_to_full,
    update_passenger_load,
)


def test_flight_time_ceil_rule(config):
    t2 = config.aircraft_types["T2"]
    assert flight_minutes(235, t2.speed_kmh) == 65
    assert flight_minutes(220, t2.speed_kmh) == 60


def test_fuel_accumulates_across_non_refuel_stops(config):
    t2 = config.aircraft_types["T2"]
    after_first = fuel_after_leg(t2.tank_capacity_kg, 200, t2)
    assert after_first == 650
    assert not can_take_leg(after_first, 201, t2)
    assert can_take_leg(t2.tank_capacity_kg, 201, t2)


def test_refuel_restores_full_tank(config):
    t2 = config.aircraft_types["T2"]
    step = advance_fuel(t2.tank_capacity_kg, 235, "F006", True, t2, config)
    assert step.arrival_fuel_kg == 562.5
    assert step.departure_fuel_kg == refuel_to_full(t2) == 1150


def test_reserve_boundary_is_inclusive(config):
    t1 = config.aircraft_types["T1"]
    assert can_take_leg(t1.tank_capacity_kg, 250, t1)
    assert not can_take_leg(t1.tank_capacity_kg, 250.01, t1)


def test_stop_time_rules(config):
    assert minimum_stop_minutes("F020", False, config) == 10
    assert minimum_stop_minutes("F006", True, config) == 20
    with pytest.raises(ValueError):
        minimum_stop_minutes("F020", True, config)


def test_capacity_delivery_before_pickup():
    update = update_passenger_load(current_load=12, deliveries=4, pickups=4, capacity=12)
    assert update.after_delivery == 8
    assert update.after_pickup == 12
    with pytest.raises(ValueError):
        update_passenger_load(current_load=12, deliveries=0, pickups=1, capacity=12)
