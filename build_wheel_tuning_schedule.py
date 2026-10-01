#!/usr/bin/env python3
"""build wheel tuning jobs and schedule them with maintenance work."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from build_baseline_schedule import (
    TimeSlot,
    TrainState,
    as_bool,
    build_depot_slots,
    fail,
    find_free_slot,
    parse_datetime,
    parse_time,
    read_csv,
    run_template_day,
    write_csv,
)


@dataclass
class WheelState:
    location: str
    available_at: datetime
    mileage: int
    is_hot_reserve: bool
    total_trips: int = 0
    daily_trips: int = 0


@dataclass
class MileagePoint:
    at: datetime
    mileage: int


@dataclass
class ServiceBooking:
    start: datetime
    end: datetime


def first_crossing(points: list[MileagePoint], limit: int) -> datetime | None:
    # find when mileage first reaches one limit
    for point in points:
        if point.mileage >= limit:
            return point.at
    return None


def run_wheel_template_day(
    day, template: list[dict[str, object]], states: dict[str, WheelState],
    points: dict[str, list[MileagePoint]], active_ids: list[str],
) -> None:
    # assign one future day and add its mileage
    for state in states.values():
        state.daily_trips = 0
    for trip in template:
        departure = datetime.combine(day, trip["departure_clock"])
        arrival = datetime.combine(day, trip["arrival_clock"])
        if arrival <= departure:
            arrival += timedelta(days=1)
        eligible = [
            train_id for train_id in active_ids
            if states[train_id].location == trip["origin"]
            and states[train_id].available_at <= departure
            and states[train_id].daily_trips < 2
        ]
        if not eligible:
            eligible = [
                train_id for train_id in active_ids
                if states[train_id].location == trip["origin"]
                and states[train_id].available_at <= departure
            ]
        if not eligible:
            fail(f"no active train for {trip['template_trip_id']} at {departure.isoformat()}")
        train_id = min(eligible, key=lambda item: (
            states[item].daily_trips, states[item].total_trips,
            states[item].available_at, item
        ))
        state = states[train_id]
        state.location = trip["destination"]
        state.available_at = arrival
        state.mileage += trip["distance_km"]
        state.daily_trips += 1
        state.total_trips += 1
        points[train_id].append(MileagePoint(arrival, state.mileage))


def limits_are_free(
    start: datetime, end: datetime, maintenance: list[ServiceBooking],
    wheel: list[ServiceBooking], global_capacity: int, wheel_capacity: int,
) -> bool:
    # check global service and wheel machine limits
    boundaries = {start, end}
    for booking in maintenance + wheel:
        if booking.start < end and booking.end > start:
            boundaries.add(max(start, booking.start))
            boundaries.add(min(end, booking.end))
    for point in sorted(boundaries)[:-1]:
        service_count = sum(item.start <= point < item.end for item in maintenance + wheel)
        wheel_count = sum(item.start <= point < item.end for item in wheel)
        if service_count >= global_capacity or wheel_count >= wheel_capacity:
            return False
    return True


def next_limit_time(
    start: datetime, duration: timedelta, end_limit: datetime,
    maintenance: list[ServiceBooking], wheel: list[ServiceBooking],
    global_capacity: int, wheel_capacity: int,
) -> datetime | None:
    # move after the first full resource period
    candidate = start
    while candidate + duration <= end_limit:
        if limits_are_free(candidate, candidate + duration, maintenance, wheel, global_capacity, wheel_capacity):
            return candidate
        endings = [
            booking.end for booking in maintenance + wheel
            if booking.start < candidate + duration and booking.end > candidate
        ]
        if not endings:
            return None
        candidate = min(endings)
    return None


def find_wheel_slot(
    windows: list[TimeSlot], train_busy: list[TimeSlot], earliest: datetime,
    latest: datetime, duration: timedelta, maintenance: list[ServiceBooking],
    wheel: list[ServiceBooking], global_capacity: int, wheel_capacity: int,
) -> TimeSlot | None:
    # find a train gap with one free wheel machine
    for window in windows:
        candidate = max(window.start, earliest)
        end_limit = min(window.end, latest)
        while candidate + duration <= end_limit:
            blockers = [
                busy for busy in train_busy
                if busy.start < candidate + duration and busy.end > candidate
            ]
            if blockers:
                candidate = min(item.end for item in blockers)
                continue
            resource_time = next_limit_time(
                candidate, duration, end_limit, maintenance, wheel, global_capacity, wheel_capacity
            )
            if resource_time is None:
                break
            candidate = resource_time
            blockers = [
                busy for busy in train_busy
                if busy.start < candidate + duration and busy.end > candidate
            ]
            if not blockers:
                return TimeSlot(candidate, candidate + duration)
    return None


def load_rows(maintenance: list[ServiceBooking], wheel: list[ServiceBooking]) -> list[dict[str, object]]:
    # write combined depot load intervals
    boundaries = sorted({point for booking in maintenance + wheel for point in (booking.start, booking.end)})
    rows: list[dict[str, object]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        service_count = sum(item.start <= start < item.end for item in maintenance + wheel)
        wheel_count = sum(item.start <= start < item.end for item in wheel)
        if service_count:
            rows.append({
                "start": start.isoformat(timespec="minutes"),
                "end": end.isoformat(timespec="minutes"),
                "trains_on_service": service_count,
                "wheel_tuning_trains": wheel_count,
            })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/case_config.json"))
    parser.add_argument("--trains", type=Path, default=Path("data/trains.csv"))
    parser.add_argument("--trips", type=Path, default=Path("data/trips.csv"))
    parser.add_argument("--template", type=Path, default=Path("data/trips_template.csv"))
    parser.add_argument(
        "--maintenance-schedule", type=Path,
        default=Path("data/resource_maintenance_schedule.csv"),
    )
    parser.add_argument("--forecast-days", type=int, default=730)
    parser.add_argument("--jobs-output", type=Path, default=Path("data/wheel_tuning_jobs.csv"))
    parser.add_argument(
        "--schedule-output", type=Path,
        default=Path("data/wheel_tuning_schedule.csv"),
    )
    parser.add_argument(
        "--load-output", type=Path,
        default=Path("data/depot_combined_load.csv"),
    )
    parser.add_argument(
        "--summary-output", type=Path,
        default=Path("data/wheel_tuning_summary.csv"),
    )
    args = parser.parse_args()
    if args.forecast_days < 1:
        fail("forecast-days must be at least 1")

    # load wheel rules and depot limit
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        wheel_config = config["wheel_tuning"]
        depot_city = config["route"]["destination"]
        global_capacity = int(config["depot_resources"]["max_concurrent_trains_on_service"])
        interval = int(wheel_config["interval_km"])
        wheel_capacity = int(wheel_config["capacity_trains"])
        cars_per_train = int(wheel_config["cars_per_train"])
        wheelsets_per_train = int(wheel_config["wheelsets_per_train"])
        tandem_wheels_per_setup = int(wheel_config["tandem_wheels_per_setup"])
        hours_per_car = float(wheel_config["duration_hours_per_car"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        fail(f"cannot read required configuration: {error}")
    if min(interval, global_capacity, wheel_capacity, cars_per_train, wheelsets_per_train, tandem_wheels_per_setup) < 1 or hours_per_car <= 0:
        fail("wheel_tuning has invalid values")
    if wheelsets_per_train % tandem_wheels_per_setup:
        fail("wheelsets_per_train must fit the tandem setup")
    duration = timedelta(hours=cars_per_train * hours_per_car)

    train_rows = read_csv(args.trains, {
        "train_id", "initial_location", "initial_mileage_km", "is_hot_reserve"
    })
    trains: dict[str, dict[str, object]] = {}
    for row in train_rows:
        train_id = row["train_id"]
        if not train_id or train_id in trains:
            fail(f"duplicate or empty train id: {train_id!r}")
        try:
            mileage = int(row["initial_mileage_km"])
        except ValueError:
            fail(f"train {train_id}: invalid initial mileage")
        trains[train_id] = {
            "initial_location": row["initial_location"], "initial_mileage_km": mileage,
            "is_hot_reserve": as_bool(row["is_hot_reserve"], "is_hot_reserve"),
        }

    trip_rows = read_csv(args.trips, {
        "trip_id", "train_id", "departure_time", "arrival_time", "origin", "destination", "distance_km"
    })
    real_trips: list[dict[str, object]] = []
    for row in trip_rows:
        if row["train_id"] not in trains:
            fail(f"trip {row['trip_id']}: unknown train")
        departure = parse_datetime(row["departure_time"], "departure_time")
        arrival = parse_datetime(row["arrival_time"], "arrival_time")
        try:
            distance = int(row["distance_km"])
        except ValueError:
            fail(f"trip {row['trip_id']}: invalid distance")
        if arrival <= departure or distance <= 0:
            fail(f"trip {row['trip_id']}: invalid route or time")
        real_trips.append({
            "train_id": row["train_id"], "origin": row["origin"], "destination": row["destination"],
            "departure": departure, "arrival": arrival, "distance_km": distance,
        })
    if not real_trips:
        fail("trips.csv must contain at least one trip")

    template_rows = read_csv(args.template, {
        "template_trip_id", "departure_time", "arrival_time", "origin", "destination", "distance_km"
    })
    template: list[dict[str, object]] = []
    for row in template_rows:
        try:
            distance = int(row["distance_km"])
        except ValueError:
            fail(f"template trip {row['template_trip_id']}: invalid distance")
        template.append({
            **row, "distance_km": distance,
            "departure_clock": parse_time(row["departure_time"], "template departure time"),
            "arrival_clock": parse_time(row["arrival_time"], "template arrival time"),
        })
    if not template:
        fail("trips_template.csv must contain at least one trip")
    template.sort(key=lambda item: (item["departure_clock"], item["template_trip_id"]))

    scenario_start = min(trip["departure"] for trip in real_trips).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    states = {
        train_id: WheelState(
            train["initial_location"], scenario_start, train["initial_mileage_km"],
            train["is_hot_reserve"]
        )
        for train_id, train in trains.items()
    }
    points = {
        train_id: [MileagePoint(scenario_start, train["initial_mileage_km"])]
        for train_id, train in trains.items()
    }
    trips_by_train: dict[str, list[dict[str, object]]] = defaultdict(list)
    for trip in sorted(real_trips, key=lambda item: (item["departure"], item["train_id"])):
        state = states[trip["train_id"]]
        if state.location != trip["origin"] or state.available_at > trip["departure"]:
            fail(f"real trip for {trip['train_id']}: train state does not match its route")
        state.location, state.available_at = trip["destination"], trip["arrival"]
        state.mileage += trip["distance_km"]
        state.total_trips += 1
        points[trip["train_id"]].append(MileagePoint(trip["arrival"], state.mileage))
        trips_by_train[trip["train_id"]].append(trip)

    active_ids = sorted(train_id for train_id, train in trains.items() if not train["is_hot_reserve"])
    last_real_day = max(trip["departure"].date() for trip in real_trips)
    planning_end = scenario_start.date() + timedelta(days=args.forecast_days - 1)
    day = last_real_day + timedelta(days=1)
    # build the same future movement as maintenance jobs
    while day <= planning_end:
        run_wheel_template_day(day, template, states, points, active_ids)
        day += timedelta(days=1)
    for train_trips in trips_by_train.values():
        train_trips.sort(key=lambda item: item["departure"])

    wheel_jobs: list[dict[str, object]] = []
    for train_id, train in trains.items():
        initial = train["initial_mileage_km"]
        horizon_mileage = points[train_id][-1].mileage
        target = (initial // interval + 1) * interval
        while target - interval <= horizon_mileage:
            earliest = scenario_start if initial >= target - interval else first_crossing(
                points[train_id], target - interval
            )
            if earliest is not None:
                wheel_jobs.append({
                    "train_id": train_id, "target_mileage_km": target, "earliest": earliest,
                    "is_hot_reserve": train["is_hot_reserve"],
                })
            target += interval

    required = {
        job["train_id"]: max(
            [item["target_mileage_km"] for item in wheel_jobs if item["train_id"] == job["train_id"] and not item["is_hot_reserve"]]
        )
        for job in wheel_jobs if not job["is_hot_reserve"]
    }
    # extend only until every wheel job gets a mileage deadline
    while any(states[train_id].mileage < target for train_id, target in required.items()):
        run_wheel_template_day(day, template, states, points, active_ids)
        day += timedelta(days=1)
    horizon_end = max(
        (first_crossing(points[job["train_id"]], job["target_mileage_km"])
         for job in wheel_jobs if not job["is_hot_reserve"]),
        default=scenario_start,
    )

    job_rows: list[dict[str, object]] = []
    for number, job in enumerate(sorted(wheel_jobs, key=lambda item: (item["train_id"], item["target_mileage_km"])), start=1):
        deadline = None if job["is_hot_reserve"] else first_crossing(
            points[job["train_id"]], job["target_mileage_km"]
        )
        job_rows.append({
            "wheel_job_id": f"WHEEL-{number:06d}", "train_id": job["train_id"],
            "target_mileage_km": job["target_mileage_km"],
            "earliest_allowed_at": job["earliest"].isoformat(timespec="minutes"),
            "latest_allowed_at": "" if deadline is None else deadline.isoformat(timespec="minutes"),
            "duration_hours": cars_per_train * hours_per_car, "cars_per_train": cars_per_train,
            "wheelsets_per_train": wheelsets_per_train,
            "tandem_wheels_per_setup": tandem_wheels_per_setup,
            "setups_per_train": wheelsets_per_train // tandem_wheels_per_setup,
            "status": "blocked_by_hot_reserve" if job["is_hot_reserve"] else "pending",
            "blocking_reason": "hot_reserve" if job["is_hot_reserve"] else "",
        })
    write_csv(args.jobs_output, [
        "wheel_job_id", "train_id", "target_mileage_km", "earliest_allowed_at",
        "latest_allowed_at", "duration_hours", "cars_per_train", "wheelsets_per_train",
        "tandem_wheels_per_setup", "setups_per_train",
        "status", "blocking_reason"
    ], job_rows)

    maintenance_rows = read_csv(args.maintenance_schedule, {
        "train_id", "scheduled_start", "scheduled_end", "status"
    })
    maintenance: list[ServiceBooking] = []
    train_busy: dict[str, list[TimeSlot]] = defaultdict(list)
    for row in maintenance_rows:
        if row["status"] != "scheduled":
            continue
        start = parse_datetime(row["scheduled_start"], "maintenance scheduled_start")
        end = parse_datetime(row["scheduled_end"], "maintenance scheduled_end")
        maintenance.append(ServiceBooking(start, end))
        train_busy[row["train_id"]].append(TimeSlot(start, end))
    for slots in train_busy.values():
        slots.sort(key=lambda item: item.start)
    depot_slots = {
        train_id: build_depot_slots(
            train_id, train["initial_location"], scenario_start,
            trips_by_train[train_id], depot_city, horizon_end,
        )
        for train_id, train in trains.items()
    }

    wheel_bookings: list[ServiceBooking] = []
    schedule_rows: list[dict[str, object]] = []
    ordered = sorted(job_rows, key=lambda job: (
        job["latest_allowed_at"] or "9999", job["train_id"], job["wheel_job_id"]
    ))
    # place wheel tuning before or after other service work
    for number, job in enumerate(ordered, start=1):
        status, reason, slot = "unscheduled", "", None
        earliest = parse_datetime(job["earliest_allowed_at"], "earliest_allowed_at")
        latest = None if not job["latest_allowed_at"] else parse_datetime(
            job["latest_allowed_at"], "latest_allowed_at"
        )
        job_duration = timedelta(hours=float(job["duration_hours"]))
        if job["status"] == "blocked_by_hot_reserve":
            reason = "hot_reserve"
        elif latest is None:
            reason = "missing_deadline"
        else:
            slot = find_wheel_slot(
                depot_slots[job["train_id"]], train_busy[job["train_id"]], earliest, latest,
                job_duration, maintenance, wheel_bookings, global_capacity, wheel_capacity,
            )
            if slot is None:
                raw_train_slot = find_free_slot(
                    depot_slots[job["train_id"]], [], earliest, latest, job_duration
                )
                train_slot = find_free_slot(
                    depot_slots[job["train_id"]], train_busy[job["train_id"]], earliest, latest, job_duration
                )
                if raw_train_slot is None:
                    reason = "no_passenger_window_before_deadline"
                elif train_slot is None:
                    reason = "maintenance_or_wheel_overlap"
                else:
                    machine_slot = find_wheel_slot(
                        depot_slots[job["train_id"]], train_busy[job["train_id"]], earliest, latest,
                        job_duration, [], wheel_bookings, global_capacity, wheel_capacity,
                    )
                    reason = "no_wheel_machine_capacity_before_deadline" if machine_slot is None else "no_service_capacity_before_deadline"
            else:
                status = "scheduled"
                train_busy[job["train_id"]].append(slot)
                train_busy[job["train_id"]].sort(key=lambda item: item.start)
                wheel_bookings.append(ServiceBooking(slot.start, slot.end))
        schedule_rows.append({
            "wheel_schedule_id": f"WSCH-{number:06d}", "wheel_job_id": job["wheel_job_id"],
            "train_id": job["train_id"], "target_mileage_km": job["target_mileage_km"],
            "scheduled_start": "" if slot is None else slot.start.isoformat(timespec="minutes"),
            "scheduled_end": "" if slot is None else slot.end.isoformat(timespec="minutes"),
            "duration_hours": job["duration_hours"], "status": status, "reason": reason,
            "earliest_allowed_at": job["earliest_allowed_at"],
            "latest_allowed_at": job["latest_allowed_at"],
        })
    write_csv(args.schedule_output, [
        "wheel_schedule_id", "wheel_job_id", "train_id", "target_mileage_km",
        "scheduled_start", "scheduled_end", "duration_hours", "status", "reason",
        "earliest_allowed_at", "latest_allowed_at"
    ], schedule_rows)

    combined_load = load_rows(maintenance, wheel_bookings)
    write_csv(args.load_output, ["start", "end", "trains_on_service", "wheel_tuning_trains"], combined_load)
    summary_rows = [{
        "total_jobs": len(schedule_rows),
        "scheduled_jobs": sum(row["status"] == "scheduled" for row in schedule_rows),
        "unscheduled_jobs": sum(row["status"] == "unscheduled" for row in schedule_rows),
        "blocked_hot_reserve_jobs": sum(row["reason"] == "hot_reserve" for row in schedule_rows),
        "passenger_window_conflicts": sum(row["reason"] == "no_passenger_window_before_deadline" for row in schedule_rows),
        "maintenance_overlap_conflicts": sum(row["reason"] == "maintenance_or_wheel_overlap" for row in schedule_rows),
        "wheel_machine_conflicts": sum(row["reason"] == "no_wheel_machine_capacity_before_deadline" for row in schedule_rows),
        "service_capacity_conflicts": sum(row["reason"] == "no_service_capacity_before_deadline" for row in schedule_rows),
        "peak_wheel_tuning_trains": max((int(row["wheel_tuning_trains"]) for row in combined_load), default=0),
        "peak_trains_on_service": max((int(row["trains_on_service"]) for row in combined_load), default=0),
    }]
    write_csv(args.summary_output, list(summary_rows[0]), summary_rows)
    print(f"created {args.jobs_output}: {len(job_rows)} wheel tuning jobs")
    print(f"created {args.schedule_output}: {len(schedule_rows)} wheel tuning rows")
    print(f"created {args.summary_output}: 1 summary row")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
