#!/usr/bin/env python3
"""build a simple maintenance schedule without depot capacity limits."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path


@dataclass
class TrainState:
    location: str
    available_at: datetime
    total_trips: int = 0
    daily_trips: int = 0


@dataclass
class TimeSlot:
    start: datetime
    end: datetime


def fail(message: str) -> None:
    # stop with a clear data error
    raise ValueError(message)


def read_csv(path: Path, fields: set[str]) -> list[dict[str, str]]:
    # read rows and check required columns
    try:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                fail(f"{path}: missing CSV header")
            missing = fields - set(reader.fieldnames)
            if missing:
                fail(f"{path}: missing fields: {', '.join(sorted(missing))}")
            return list(reader)
    except OSError as error:
        fail(f"cannot read {path}: {error}")


def parse_datetime(value: str, field: str) -> datetime:
    # read one date and time value
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        fail(f"invalid {field}: {value!r} ({error})")


def parse_time(value: str, field: str) -> time:
    # read one time without a date
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        fail(f"invalid {field}: {value!r} ({error})")


def as_bool(value: str, field: str) -> bool:
    # accept only true or false values
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    fail(f"invalid {field}: {value!r}")


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    # write rows with unix line endings
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def run_template_day(
    day: date, template: list[dict[str, object]], states: dict[str, TrainState],
    trips_by_train: dict[str, list[dict[str, object]]], active_ids: list[str],
) -> None:
    # assign one template day to active trains
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
            # allow more than two trips when needed
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
        # add the selected future trip
        state = states[train_id]
        state.location = trip["destination"]
        state.available_at = arrival
        state.daily_trips += 1
        state.total_trips += 1
        trips_by_train[train_id].append({
            "train_id": train_id, "origin": trip["origin"], "destination": trip["destination"],
            "departure": departure, "arrival": arrival,
        })


def build_depot_slots(
    train_id: str, initial_location: str, scenario_start: datetime,
    trips: list[dict[str, object]], depot_city: str, horizon_end: datetime,
) -> list[TimeSlot]:
    # find train time in the depot city
    slots: list[TimeSlot] = []
    current_location = initial_location
    current_time = scenario_start
    for trip in trips:
        if current_location == depot_city and current_time < trip["departure"]:
            slots.append(TimeSlot(current_time, trip["departure"]))
        current_location = trip["destination"]
        current_time = trip["arrival"]
    if current_location == depot_city and current_time < horizon_end:
        slots.append(TimeSlot(current_time, horizon_end))
    return slots


def find_free_slot(
    windows: list[TimeSlot], occupied: list[TimeSlot], earliest: datetime,
    latest: datetime, duration: timedelta,
) -> TimeSlot | None:
    # find the first gap before the job deadline
    for window in windows:
        candidate = max(window.start, earliest)
        end_limit = min(window.end, latest)
        if candidate + duration > end_limit:
            continue
        for busy in occupied:
            if busy.end <= candidate:
                continue
            if busy.start >= end_limit:
                break
            if candidate + duration <= busy.start:
                break
            candidate = max(candidate, busy.end)
            if candidate + duration > end_limit:
                break
        if candidate + duration <= end_limit:
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
        "--schedule-output", type=Path,
        default=Path("data/baseline_maintenance_schedule.csv"),
    )
    parser.add_argument(
        "--summary-output", type=Path,
        default=Path("data/baseline_schedule_summary.csv"),
    )
    args = parser.parse_args()

    # load the city that can access the depot
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        depot_city = config["route"]["destination"]
        cycles = {item["code"]: item for item in config["maintenance_cycles"]}
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        fail(f"cannot read required configuration: {error}")

    train_rows = read_csv(args.trains, {
        "train_id", "initial_location", "is_hot_reserve"
    })
    trains: dict[str, dict[str, object]] = {}
    for row in train_rows:
        if not row["train_id"] or row["train_id"] in trains:
            fail(f"duplicate or empty train id: {row['train_id']!r}")
        trains[row["train_id"]] = {
            "initial_location": row["initial_location"],
            "is_hot_reserve": as_bool(row["is_hot_reserve"], "is_hot_reserve"),
        }

    job_rows = read_csv(args.jobs, {
        "job_id", "train_id", "cycle_code", "target_mileage_km", "earliest_allowed_at",
        "latest_allowed_at", "downtime_hours", "splittable", "location", "status",
        "blocking_reason"
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
    # build future trips through the last job deadline
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
    schedule_rows: list[dict[str, object]] = []
    ordered_jobs = sorted(jobs, key=lambda job: (
        job["latest"] or datetime.max,
        (job["latest"] or datetime.max) - job["earliest"],
        -job["duration"].total_seconds(), -job["interval_km"],
        job["train_id"], job["job_id"],
    ))

    # place urgent jobs before wider windows
    for number, job in enumerate(ordered_jobs, start=1):
        status, reason, slot = "unscheduled", "", None
        if job["status"] == "blocked_by_hot_reserve" or trains[job["train_id"]]["is_hot_reserve"]:
            reason = "hot_reserve"
        elif job["latest"] is None:
            reason = "missing_deadline"
        else:
            slot = find_free_slot(
                depot_slots[job["train_id"]], occupied[job["train_id"]],
                job["earliest"], job["latest"], job["duration"],
            )
            if slot is None:
                reason = "no_depot_window_before_deadline"
            else:
                status = "scheduled"
                occupied[job["train_id"]].append(slot)
                occupied[job["train_id"]].sort(key=lambda item: item.start)
        schedule_rows.append({
            "schedule_id": f"SCH-{number:06d}", "job_id": job["job_id"],
            "train_id": job["train_id"], "cycle_code": job["cycle_code"],
            "location": job["location"],
            "scheduled_start": "" if slot is None else slot.start.isoformat(timespec="minutes"),
            "scheduled_end": "" if slot is None else slot.end.isoformat(timespec="minutes"),
            "duration_hours": job["downtime_hours"], "status": status, "reason": reason,
            "earliest_allowed_at": job["earliest_allowed_at"],
            "latest_allowed_at": job["latest_allowed_at"],
            "target_mileage_km": job["target_mileage_km"],
        })

    fields = [
        "schedule_id", "job_id", "train_id", "cycle_code", "location", "scheduled_start",
        "scheduled_end", "duration_hours", "status", "reason", "earliest_allowed_at",
        "latest_allowed_at", "target_mileage_km"
    ]
    write_csv(args.schedule_output, fields, schedule_rows)
    summary_rows: list[dict[str, object]] = []
    for scope, rows in [("all", schedule_rows)] + [
        (cycle, [row for row in schedule_rows if row["cycle_code"] == cycle])
        for cycle in sorted({row["cycle_code"] for row in schedule_rows})
    ]:
        summary_rows.append({
            "scope": scope, "total_jobs": len(rows),
            "scheduled_jobs": sum(row["status"] == "scheduled" for row in rows),
            "unscheduled_jobs": sum(row["status"] == "unscheduled" for row in rows),
            "blocked_hot_reserve_jobs": sum(row["reason"] == "hot_reserve" for row in rows),
            "scheduled_downtime_hours": sum(
                float(row["duration_hours"]) for row in rows if row["status"] == "scheduled"
            ),
        })
    write_csv(args.summary_output, [
        "scope", "total_jobs", "scheduled_jobs", "unscheduled_jobs",
        "blocked_hot_reserve_jobs", "scheduled_downtime_hours"
    ], summary_rows)
    print(f"created {args.schedule_output}: {len(schedule_rows)} maintenance rows")
    print(f"created {args.summary_output}: {len(summary_rows)} summary rows")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
