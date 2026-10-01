#!/usr/bin/env python3
"""build a maintenance schedule with depot resource limits."""

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
class ResourceBooking:
    start: datetime
    end: datetime
    group: str


def resource_is_free(
    start: datetime, end: datetime, group: str, bookings: list[ResourceBooking],
    global_capacity: int, group_capacity: int,
) -> bool:
    # check every time part of the new job
    boundaries = {start, end}
    for booking in bookings:
        if booking.start < end and booking.end > start:
            boundaries.add(max(start, booking.start))
            boundaries.add(min(end, booking.end))
    ordered = sorted(boundaries)
    for point in ordered[:-1]:
        active = [booking for booking in bookings if booking.start <= point < booking.end]
        if len(active) >= global_capacity:
            return False
        if sum(booking.group == group for booking in active) >= group_capacity:
            return False
    return True


def next_resource_time(
    start: datetime, duration: timedelta, end_limit: datetime, group: str,
    bookings: list[ResourceBooking], global_capacity: int, group_capacity: int,
) -> datetime | None:
    # move after the first blocking service
    candidate = start
    while candidate + duration <= end_limit:
        if resource_is_free(
            candidate, candidate + duration, group, bookings, global_capacity, group_capacity
        ):
            return candidate
        endings = [
            booking.end for booking in bookings
            if booking.start < candidate + duration and booking.end > candidate
        ]
        if not endings:
            return None
        candidate = min(endings)
    return None


def find_resource_slot(
    windows: list[TimeSlot], occupied: list[TimeSlot], earliest: datetime,
    latest: datetime, duration: timedelta, group: str, bookings: list[ResourceBooking],
    global_capacity: int, group_capacity: int,
) -> TimeSlot | None:
    # find one free train and depot resource gap
    for window in windows:
        candidate = max(window.start, earliest)
        end_limit = min(window.end, latest)
        while candidate + duration <= end_limit:
            train_blockers = [
                busy for busy in occupied
                if busy.start < candidate + duration and busy.end > candidate
            ]
            if train_blockers:
                candidate = min(busy.end for busy in train_blockers)
                continue
            resource_time = next_resource_time(
                candidate, duration, end_limit, group, bookings, global_capacity, group_capacity
            )
            if resource_time is None:
                break
            candidate = resource_time
            train_blockers = [
                busy for busy in occupied
                if busy.start < candidate + duration and busy.end > candidate
            ]
            if not train_blockers:
                return TimeSlot(candidate, candidate + duration)
    return None


def resource_load_rows(bookings: list[ResourceBooking], groups: list[str]) -> list[dict[str, object]]:
    # make load intervals between service events
    boundaries = sorted({point for booking in bookings for point in (booking.start, booking.end)})
    rows: list[dict[str, object]] = []
    for start, end in zip(boundaries, boundaries[1:]):
        active = [booking for booking in bookings if booking.start <= start < booking.end]
        if not active:
            continue
        row: dict[str, object] = {
            "start": start.isoformat(timespec="minutes"),
            "end": end.isoformat(timespec="minutes"),
            "trains_on_service": len(active),
        }
        for group in groups:
            row[f"{group}_trains"] = sum(booking.group == group for booking in active)
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/case_config.json"))
    parser.add_argument("--trains", type=Path, default=Path("data/trains.csv"))
    parser.add_argument("--trips", type=Path, default=Path("data/trips.csv"))
    parser.add_argument("--template", type=Path, default=Path("data/trips_template.csv"))
    parser.add_argument("--jobs", type=Path, default=Path("data/maintenance_jobs.csv"))
    parser.add_argument(
        "--schedule-output", type=Path,
        default=Path("data/resource_maintenance_schedule.csv"),
    )
    parser.add_argument(
        "--summary-output", type=Path,
        default=Path("data/resource_schedule_summary.csv"),
    )
    parser.add_argument(
        "--load-output", type=Path,
        default=Path("data/depot_resource_load.csv"),
    )
    args = parser.parse_args()

    # load depot capacities and cycle groups
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        depot_city = config["route"]["destination"]
        resource_config = config["depot_resources"]
        global_capacity = int(resource_config["max_concurrent_trains_on_service"])
        positions = resource_config["positions"]
        cycles = {item["code"]: item for item in config["maintenance_cycles"]}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        fail(f"cannot read required configuration: {error}")
    if global_capacity < 1:
        fail("max_concurrent_trains_on_service must be positive")
    cycle_groups: dict[str, str] = {}
    group_capacities: dict[str, int] = {}
    for group, details in positions.items():
        capacity = int(details["capacity_trains"])
        if capacity < 1:
            fail(f"resource group {group} has invalid capacity")
        group_capacities[group] = capacity
        for cycle in details["supported_cycles"]:
            if cycle in cycle_groups:
                fail(f"cycle {cycle} has two resource groups")
            cycle_groups[cycle] = group

    train_rows = read_csv(args.trains, {
        "train_id", "initial_location", "is_hot_reserve"
    })
    trains: dict[str, dict[str, object]] = {}
    for row in train_rows:
        train_id = row["train_id"]
        if not train_id or train_id in trains:
            fail(f"duplicate or empty train id: {train_id!r}")
        trains[train_id] = {
            "initial_location": row["initial_location"],
            "is_hot_reserve": as_bool(row["is_hot_reserve"], "is_hot_reserve"),
        }

    job_rows = read_csv(args.jobs, {
        "job_id", "train_id", "cycle_code", "target_mileage_km", "earliest_allowed_at",
        "latest_allowed_at", "downtime_hours", "location", "status", "blocking_reason"
    })
    jobs: list[dict[str, object]] = []
    for row in job_rows:
        if row["train_id"] not in trains:
            fail(f"job {row['job_id']}: unknown train")
        if row["cycle_code"] not in cycles:
            fail(f"job {row['job_id']}: unknown cycle")
        try:
            duration_hours = float(row["downtime_hours"])
        except ValueError:
            fail(f"job {row['job_id']}: invalid downtime_hours")
        earliest = parse_datetime(row["earliest_allowed_at"], "earliest_allowed_at")
        latest = None if not row["latest_allowed_at"] else parse_datetime(
            row["latest_allowed_at"], "latest_allowed_at"
        )
        if duration_hours <= 0 or latest is not None and latest < earliest:
            fail(f"job {row['job_id']}: invalid time window")
        jobs.append({
            **row, "earliest": earliest, "latest": latest,
            "duration": timedelta(hours=duration_hours),
            "interval_km": cycles[row["cycle_code"]]["interval_km"],
        })

    trip_rows = read_csv(args.trips, {
        "trip_id", "train_id", "departure_time", "arrival_time", "origin", "destination"
    })
    real_trips: list[dict[str, object]] = []
    for row in trip_rows:
        if row["train_id"] not in trains:
            fail(f"trip {row['trip_id']}: unknown train")
        departure = parse_datetime(row["departure_time"], "departure_time")
        arrival = parse_datetime(row["arrival_time"], "arrival_time")
        if arrival <= departure:
            fail(f"trip {row['trip_id']}: invalid times")
        real_trips.append({
            "train_id": row["train_id"], "origin": row["origin"],
            "destination": row["destination"], "departure": departure, "arrival": arrival,
        })
    if not real_trips:
        fail("trips.csv must contain at least one trip")

    template_rows = read_csv(args.template, {
        "template_trip_id", "departure_time", "arrival_time", "origin", "destination"
    })
    template: list[dict[str, object]] = []
    for row in template_rows:
        template.append({
            **row,
            "departure_clock": parse_time(row["departure_time"], "template departure time"),
            "arrival_clock": parse_time(row["arrival_time"], "template arrival time"),
        })
    if not template:
        fail("trips_template.csv must contain at least one trip")
    template.sort(key=lambda item: (item["departure_clock"], item["template_trip_id"]))

    scenario_start = min(trip["departure"] for trip in real_trips).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    horizon_end = max(
        (job["latest"] for job in jobs if job["latest"] is not None),
        default=scenario_start,
    )
    states = {
        train_id: TrainState(train["initial_location"], scenario_start)
        for train_id, train in trains.items()
    }
    trips_by_train: dict[str, list[dict[str, object]]] = defaultdict(list)
    for trip in sorted(real_trips, key=lambda item: (item["departure"], item["train_id"])):
        state = states[trip["train_id"]]
        if state.location != trip["origin"] or state.available_at > trip["departure"]:
            fail(f"real trip for {trip['train_id']}: train state does not match its route")
        state.location, state.available_at = trip["destination"], trip["arrival"]
        state.total_trips += 1
        trips_by_train[trip["train_id"]].append(trip)

    active_ids = sorted(train_id for train_id, train in trains.items() if not train["is_hot_reserve"])
    last_real_day = max(trip["departure"].date() for trip in real_trips)
    day = last_real_day + timedelta(days=1)
    # build future trips through the last deadline
    while day <= horizon_end.date():
        run_template_day(day, template, states, trips_by_train, active_ids)
        day += timedelta(days=1)
    for train_trips in trips_by_train.values():
        train_trips.sort(key=lambda item: item["departure"])

    depot_slots = {
        train_id: build_depot_slots(
            train_id, train["initial_location"], scenario_start,
            trips_by_train[train_id], depot_city, horizon_end,
        )
        for train_id, train in trains.items()
    }
    occupied: dict[str, list[TimeSlot]] = defaultdict(list)
    bookings: list[ResourceBooking] = []
    ordered_jobs = sorted(jobs, key=lambda job: (
        job["latest"] or datetime.max,
        (job["latest"] or datetime.max) - job["earliest"],
        -job["duration"].total_seconds(), -job["interval_km"],
        job["train_id"], job["job_id"],
    ))
    schedule_rows: list[dict[str, object]] = []

    # place urgent jobs with both train and depot limits
    for number, job in enumerate(ordered_jobs, start=1):
        status, reason, slot, group = "unscheduled", "", None, cycle_groups.get(job["cycle_code"], "")
        if job["status"] == "blocked_by_hot_reserve" or trains[job["train_id"]]["is_hot_reserve"]:
            reason = "hot_reserve"
        elif job["latest"] is None:
            reason = "missing_deadline"
        elif not group:
            reason = "unsupported_cycle"
        else:
            slot = find_resource_slot(
                depot_slots[job["train_id"]], occupied[job["train_id"]],
                job["earliest"], job["latest"], job["duration"], group, bookings,
                global_capacity, group_capacities[group],
            )
            if slot is None:
                train_slot = find_free_slot(
                    depot_slots[job["train_id"]], occupied[job["train_id"]],
                    job["earliest"], job["latest"], job["duration"],
                )
                reason = "no_depot_window_before_deadline" if train_slot is None else "no_resource_capacity_before_deadline"
            else:
                status = "scheduled"
                occupied[job["train_id"]].append(slot)
                occupied[job["train_id"]].sort(key=lambda item: item.start)
                bookings.append(ResourceBooking(slot.start, slot.end, group))
        schedule_rows.append({
            "schedule_id": f"RSCH-{number:06d}", "job_id": job["job_id"],
            "train_id": job["train_id"], "cycle_code": job["cycle_code"],
            "resource_group": group, "location": job["location"],
            "scheduled_start": "" if slot is None else slot.start.isoformat(timespec="minutes"),
            "scheduled_end": "" if slot is None else slot.end.isoformat(timespec="minutes"),
            "duration_hours": job["downtime_hours"], "status": status, "reason": reason,
            "earliest_allowed_at": job["earliest_allowed_at"],
            "latest_allowed_at": job["latest_allowed_at"],
            "target_mileage_km": job["target_mileage_km"],
        })

    schedule_fields = [
        "schedule_id", "job_id", "train_id", "cycle_code", "resource_group", "location",
        "scheduled_start", "scheduled_end", "duration_hours", "status", "reason",
        "earliest_allowed_at", "latest_allowed_at", "target_mileage_km"
    ]
    write_csv(args.schedule_output, schedule_fields, schedule_rows)
    load_rows = resource_load_rows(bookings, sorted(group_capacities))
    load_fields = ["start", "end", "trains_on_service"] + [
        f"{group}_trains" for group in sorted(group_capacities)
    ]
    write_csv(args.load_output, load_fields, load_rows)
    peak_global = max((int(row["trains_on_service"]) for row in load_rows), default=0)
    summary_rows = [{
        "scope": "all", "total_jobs": len(schedule_rows),
        "scheduled_jobs": sum(row["status"] == "scheduled" for row in schedule_rows),
        "unscheduled_jobs": sum(row["status"] == "unscheduled" for row in schedule_rows),
        "resource_conflicts": sum(row["reason"] == "no_resource_capacity_before_deadline" for row in schedule_rows),
        "peak_trains_on_service": peak_global,
    }]
    for group in sorted(group_capacities):
        summary_rows.append({
            "scope": group,
            "total_jobs": sum(row["resource_group"] == group for row in schedule_rows),
            "scheduled_jobs": sum(row["resource_group"] == group and row["status"] == "scheduled" for row in schedule_rows),
            "unscheduled_jobs": sum(row["resource_group"] == group and row["status"] == "unscheduled" for row in schedule_rows),
            "resource_conflicts": sum(row["resource_group"] == group and row["reason"] == "no_resource_capacity_before_deadline" for row in schedule_rows),
            "peak_trains_on_service": max((int(row[f"{group}_trains"]) for row in load_rows), default=0),
        })
    write_csv(args.summary_output, [
        "scope", "total_jobs", "scheduled_jobs", "unscheduled_jobs", "resource_conflicts",
        "peak_trains_on_service"
    ], summary_rows)
    print(f"created {args.schedule_output}: {len(schedule_rows)} maintenance rows")
    print(f"created {args.load_output}: {len(load_rows)} depot load rows")
    print(f"created {args.summary_output}: {len(summary_rows)} summary rows")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
