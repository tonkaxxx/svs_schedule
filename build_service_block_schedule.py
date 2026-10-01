#!/usr/bin/env python3
"""split long maintenance jobs into ordered time blocks."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from build_baseline_schedule import (
    TimeSlot, TrainState, build_depot_slots, fail, parse_datetime, parse_time,
    read_csv, run_template_day, write_csv,
)


@dataclass
class Booking:
    start: datetime
    end: datetime
    group: str


def resource_is_free(
    start: datetime, end: datetime, bookings: list[Booking],
    global_capacity: int, inspection_capacity: int,
) -> bool:
    # check service limits through the full block
    boundaries = {start, end}
    overlaps = [item for item in bookings if item.start < end and item.end > start]
    for item in overlaps:
        boundaries.add(max(start, item.start))
        boundaries.add(min(end, item.end))
    for point in sorted(boundaries)[:-1]:
        active = [item for item in overlaps if item.start <= point < item.end]
        if len(active) >= global_capacity:
            return False
        if sum(item.group == "inspection_and_service" for item in active) >= inspection_capacity:
            return False
    return True


def next_resource_time(
    start: datetime, duration: timedelta, end_limit: datetime, bookings: list[Booking],
    global_capacity: int, inspection_capacity: int,
) -> datetime | None:
    # move after the first full resource period
    candidate = start
    while candidate + duration <= end_limit:
        if resource_is_free(candidate, candidate + duration, bookings, global_capacity, inspection_capacity):
            return candidate
        endings = [item.end for item in bookings if item.start < candidate + duration and item.end > candidate]
        if not endings:
            return None
        candidate = min(endings)
    return None


def find_block_slot(
    windows: list[TimeSlot], busy: list[TimeSlot], earliest: datetime, latest: datetime,
    duration: timedelta, bookings: list[Booking], global_capacity: int, inspection_capacity: int,
) -> TimeSlot | None:
    # find one gap for a block in the depot
    for window in windows:
        candidate = max(window.start, earliest)
        end_limit = min(window.end, latest)
        while candidate + duration <= end_limit:
            blockers = [item for item in busy if item.start < candidate + duration and item.end > candidate]
            if blockers:
                candidate = min(item.end for item in blockers)
                continue
            resource_time = next_resource_time(
                candidate, duration, end_limit, bookings, global_capacity, inspection_capacity
            )
            if resource_time is None:
                break
            candidate = resource_time
            blockers = [item for item in busy if item.start < candidate + duration and item.end > candidate]
            if not blockers:
                return TimeSlot(candidate, candidate + duration)
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/case_config.json"))
    parser.add_argument("--trains", type=Path, default=Path("data/trains.csv"))
    parser.add_argument("--trips", type=Path, default=Path("data/trips.csv"))
    parser.add_argument("--template", type=Path, default=Path("data/trips_template.csv"))
    parser.add_argument("--jobs", type=Path, default=Path("data/maintenance_jobs.csv"))
    parser.add_argument(
        "--maintenance-schedule", type=Path,
        default=Path("data/resource_maintenance_schedule.csv"),
    )
    parser.add_argument("--wheel-schedule", type=Path, default=Path("data/wheel_tuning_schedule.csv"))
    parser.add_argument("--cleaning-schedule", type=Path, default=Path("data/cleaning_schedule.csv"))
    parser.add_argument("--blocks-output", type=Path, default=Path("data/service_blocks.csv"))
    parser.add_argument(
        "--schedule-output", type=Path,
        default=Path("data/service_block_schedule.csv"),
    )
    parser.add_argument(
        "--summary-output", type=Path,
        default=Path("data/service_block_summary.csv"),
    )
    args = parser.parse_args()

    # load block rule and depot capacity
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        block_config = config["service_blocks"]
        block_hours = float(block_config["max_duration_hours"])
        block_cycles = set(block_config["supported_cycles"])
        depot_city = config["route"]["destination"]
        global_capacity = int(config["depot_resources"]["max_concurrent_trains_on_service"])
        inspection_capacity = int(config["depot_resources"]["positions"]["inspection_and_service"]["capacity_trains"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        fail(f"cannot read required configuration: {error}")
    if block_hours <= 0 or global_capacity < 1 or inspection_capacity < 1:
        fail("service block configuration has invalid values")

    train_rows = read_csv(args.trains, {"train_id", "initial_location", "is_hot_reserve"})
    trains: dict[str, dict[str, object]] = {}
    for row in train_rows:
        if not row["train_id"] or row["train_id"] in trains:
            fail(f"duplicate or empty train id: {row['train_id']!r}")
        trains[row["train_id"]] = {"initial_location": row["initial_location"]}

    job_rows = read_csv(args.jobs, {
        "job_id", "train_id", "cycle_code", "earliest_allowed_at", "latest_allowed_at",
        "downtime_hours", "status", "target_mileage_km"
    })
    parent_jobs: list[dict[str, object]] = []
    for row in job_rows:
        if row["cycle_code"] not in block_cycles:
            continue
        if row["train_id"] not in trains:
            fail(f"job {row['job_id']}: unknown train")
        if row["status"] != "pending" or not row["latest_allowed_at"]:
            continue
        try:
            duration_hours = float(row["downtime_hours"])
        except ValueError:
            fail(f"job {row['job_id']}: invalid downtime")
        parent_jobs.append({
            **row,
            "earliest": parse_datetime(row["earliest_allowed_at"], "earliest_allowed_at"),
            "latest": parse_datetime(row["latest_allowed_at"], "latest_allowed_at"),
            "duration_hours_value": duration_hours,
        })
    if not parent_jobs:
        fail("no pending jobs found for service blocks")

    block_rows: list[dict[str, object]] = []
    parent_blocks: dict[str, list[dict[str, object]]] = defaultdict(list)
    for parent in sorted(parent_jobs, key=lambda item: (item["latest"], item["job_id"])):
        count = math.ceil(parent["duration_hours_value"] / block_hours)
        remaining = parent["duration_hours_value"]
        for index in range(1, count + 1):
            hours = min(block_hours, remaining)
            remaining -= hours
            row = {
                "service_block_id": f"{parent['job_id']}-B{index:02d}",
                "parent_job_id": parent["job_id"], "train_id": parent["train_id"],
                "cycle_code": parent["cycle_code"], "block_number": index, "block_count": count,
                "duration_hours": hours, "earliest_allowed_at": parent["earliest_allowed_at"],
                "latest_allowed_at": parent["latest_allowed_at"],
                "target_mileage_km": parent["target_mileage_km"],
                "strategy": block_config["strategy"],
            }
            block_rows.append(row)
            parent_blocks[parent["job_id"]].append(row)
    write_csv(args.blocks_output, [
        "service_block_id", "parent_job_id", "train_id", "cycle_code", "block_number",
        "block_count", "duration_hours", "earliest_allowed_at", "latest_allowed_at",
        "target_mileage_km", "strategy"
    ], block_rows)

    trip_rows = read_csv(args.trips, {
        "trip_id", "train_id", "departure_time", "arrival_time", "origin", "destination"
    })
    real_trips: list[dict[str, object]] = []
    for row in trip_rows:
        departure = parse_datetime(row["departure_time"], "departure_time")
        arrival = parse_datetime(row["arrival_time"], "arrival_time")
        if row["train_id"] not in trains or arrival <= departure:
            fail(f"invalid real trip: {row['trip_id']}")
        real_trips.append({
            "train_id": row["train_id"], "origin": row["origin"], "destination": row["destination"],
            "departure": departure, "arrival": arrival,
        })
    template_rows = read_csv(args.template, {
        "template_trip_id", "departure_time", "arrival_time", "origin", "destination"
    })
    template = [{
        **row,
        "departure_clock": parse_time(row["departure_time"], "template departure time"),
        "arrival_clock": parse_time(row["arrival_time"], "template arrival time"),
    } for row in template_rows]
    template.sort(key=lambda item: (item["departure_clock"], item["template_trip_id"]))

    scenario_start = min(item["departure"] for item in real_trips).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    horizon_end = max(parent["latest"] for parent in parent_jobs)
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
    active_ids = sorted(trains)
    active_ids = [train_id for train_id in active_ids if train_id not in {
        row["train_id"] for row in train_rows if row["is_hot_reserve"] == "true"
    }]
    day = max(item["departure"].date() for item in real_trips) + timedelta(days=1)
    while day <= horizon_end.date():
        run_template_day(day, template, states, trips_by_train, active_ids)
        day += timedelta(days=1)
    for items in trips_by_train.values():
        items.sort(key=lambda item: item["departure"])
    depot_slots = {
        train_id: build_depot_slots(
            train_id, train["initial_location"], scenario_start,
            trips_by_train[train_id], depot_city, horizon_end,
        )
        for train_id, train in trains.items()
    }

    fixed_rows = read_csv(args.maintenance_schedule, {
        "train_id", "cycle_code", "resource_group", "scheduled_start", "scheduled_end", "status"
    })
    train_busy: dict[str, list[TimeSlot]] = defaultdict(list)
    bookings: list[Booking] = []
    for row in fixed_rows:
        if row["status"] != "scheduled" or row["cycle_code"] in block_cycles:
            continue
        start = parse_datetime(row["scheduled_start"], "maintenance scheduled_start")
        end = parse_datetime(row["scheduled_end"], "maintenance scheduled_end")
        train_busy[row["train_id"]].append(TimeSlot(start, end))
        bookings.append(Booking(start, end, row["resource_group"]))
    for schedule_path in (args.wheel_schedule, args.cleaning_schedule):
        rows = read_csv(schedule_path, {"train_id", "scheduled_start", "scheduled_end", "status"})
        for row in rows:
            if row["status"] != "scheduled":
                continue
            start = parse_datetime(row["scheduled_start"], "scheduled_start")
            end = parse_datetime(row["scheduled_end"], "scheduled_end")
            train_busy[row["train_id"]].append(TimeSlot(start, end))
            if schedule_path == args.wheel_schedule:
                bookings.append(Booking(start, end, "wheel"))
    for busy in train_busy.values():
        busy.sort(key=lambda item: item.start)

    schedule_rows: list[dict[str, object]] = []
    completed_parents: dict[str, bool] = {}
    # schedule parent jobs by deadline and blocks in their given order
    for parent in sorted(parent_jobs, key=lambda item: (item["latest"], item["job_id"])):
        predecessor_end = parent["earliest"]
        parent_ok = True
        for block in parent_blocks[parent["job_id"]]:
            earliest = max(predecessor_end, parse_datetime(block["earliest_allowed_at"], "earliest_allowed_at"))
            latest = parse_datetime(block["latest_allowed_at"], "latest_allowed_at")
            duration = timedelta(hours=float(block["duration_hours"]))
            slot = None
            reason = ""
            if parent_ok:
                slot = find_block_slot(
                    depot_slots[block["train_id"]], train_busy[block["train_id"]], earliest, latest,
                    duration, bookings, global_capacity, inspection_capacity,
                )
                if slot is None:
                    reason = "no_block_window_before_deadline"
                    parent_ok = False
                else:
                    predecessor_end = slot.end
                    train_busy[block["train_id"]].append(slot)
                    train_busy[block["train_id"]].sort(key=lambda item: item.start)
                    bookings.append(Booking(slot.start, slot.end, "inspection_and_service"))
            else:
                reason = "previous_block_unscheduled"
            schedule_rows.append({
                **block,
                "scheduled_start": "" if slot is None else slot.start.isoformat(timespec="minutes"),
                "scheduled_end": "" if slot is None else slot.end.isoformat(timespec="minutes"),
                "status": "scheduled" if slot is not None else "unscheduled",
                "reason": reason,
            })
        completed_parents[parent["job_id"]] = parent_ok
    write_csv(args.schedule_output, [
        "service_block_id", "parent_job_id", "train_id", "cycle_code", "block_number",
        "block_count", "duration_hours", "scheduled_start", "scheduled_end", "status", "reason",
        "earliest_allowed_at", "latest_allowed_at", "target_mileage_km", "strategy"
    ], schedule_rows)
    summary_rows = [{
        "parent_jobs": len(parent_jobs),
        "completed_parent_jobs": sum(completed_parents.values()),
        "incomplete_parent_jobs": sum(not value for value in completed_parents.values()),
        "service_blocks": len(schedule_rows),
        "scheduled_blocks": sum(row["status"] == "scheduled" for row in schedule_rows),
        "unscheduled_blocks": sum(row["status"] == "unscheduled" for row in schedule_rows),
        "max_block_hours": block_hours,
        "strategy": block_config["strategy"],
    }]
    write_csv(args.summary_output, list(summary_rows[0]), summary_rows)
    print(f"created {args.blocks_output}: {len(block_rows)} service blocks")
    print(f"created {args.schedule_output}: {len(schedule_rows)} scheduled blocks")
    print(f"created {args.summary_output}: 1 summary row")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
