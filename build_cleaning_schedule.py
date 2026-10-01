#!/usr/bin/env python3
"""build cleaning and equipping jobs between passenger trips."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from build_baseline_schedule import (
    TimeSlot,
    TrainState,
    as_bool,
    fail,
    find_free_slot,
    parse_datetime,
    parse_time,
    read_csv,
    run_template_day,
    write_csv,
)


@dataclass
class FacilityBooking:
    start: datetime
    end: datetime


def overlapping_bookings(
    start: datetime, end: datetime, bookings: list[FacilityBooking]
) -> list[FacilityBooking]:
    # read only bookings close to this cleaning time
    index = bisect_left(bookings, start, key=lambda item: item.start)
    left = index
    while left and bookings[left - 1].end > start:
        left -= 1
    right = index
    while right < len(bookings) and bookings[right].start < end:
        right += 1
    return bookings[left:right]


def facility_is_free(
    start: datetime, end: datetime, bookings: list[FacilityBooking], capacity: int
) -> bool:
    # check all time parts of a cleaning job
    overlaps = overlapping_bookings(start, end, bookings)
    boundaries = {start, end}
    for booking in overlaps:
        boundaries.add(max(start, booking.start))
        boundaries.add(min(end, booking.end))
    for point in sorted(boundaries)[:-1]:
        if sum(item.start <= point < item.end for item in overlaps) >= capacity:
            return False
    return True


def next_facility_time(
    start: datetime, duration: timedelta, end_limit: datetime,
    bookings: list[FacilityBooking], capacity: int,
) -> datetime | None:
    # move after the first full cleaning facility period
    candidate = start
    while candidate + duration <= end_limit:
        if facility_is_free(candidate, candidate + duration, bookings, capacity):
            return candidate
        endings = [item.end for item in overlapping_bookings(candidate, candidate + duration, bookings)]
        if not endings:
            return None
        candidate = min(endings)
    return None


def find_cleaning_slot(
    window: TimeSlot, train_busy: list[TimeSlot], duration: timedelta,
    bookings: list[FacilityBooking], capacity: int,
) -> TimeSlot | None:
    # find a train gap with a free cleaning facility
    candidate = window.start
    while candidate + duration <= window.end:
        blockers = [
            busy for busy in train_busy
            if busy.start < candidate + duration and busy.end > candidate
        ]
        if blockers:
            candidate = min(item.end for item in blockers)
            continue
        facility_time = next_facility_time(candidate, duration, window.end, bookings, capacity)
        if facility_time is None:
            return None
        candidate = facility_time
        blockers = [
            busy for busy in train_busy
            if busy.start < candidate + duration and busy.end > candidate
        ]
        if not blockers:
            return TimeSlot(candidate, candidate + duration)
    return None


def load_rows(bookings: dict[str, list[FacilityBooking]]) -> list[dict[str, object]]:
    # make load intervals for each cleaning city
    rows: list[dict[str, object]] = []
    for city, city_bookings in sorted(bookings.items()):
        boundaries = sorted({point for booking in city_bookings for point in (booking.start, booking.end)})
        for start, end in zip(boundaries, boundaries[1:]):
            active = sum(booking.start <= start < booking.end for booking in city_bookings)
            if active:
                rows.append({
                    "location": city, "start": start.isoformat(timespec="minutes"),
                    "end": end.isoformat(timespec="minutes"), "trains_cleaning": active,
                })
    return sorted(rows, key=lambda row: (row["start"], row["location"]))


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
    parser.add_argument(
        "--wheel-schedule", type=Path,
        default=Path("data/wheel_tuning_schedule.csv"),
    )
    parser.add_argument("--forecast-days", type=int, default=730)
    parser.add_argument("--jobs-output", type=Path, default=Path("data/cleaning_jobs.csv"))
    parser.add_argument("--schedule-output", type=Path, default=Path("data/cleaning_schedule.csv"))
    parser.add_argument("--load-output", type=Path, default=Path("data/cleaning_facility_load.csv"))
    parser.add_argument("--summary-output", type=Path, default=Path("data/cleaning_summary.csv"))
    args = parser.parse_args()
    if args.forecast_days < 1:
        fail("forecast-days must be at least 1")

    # load cleaning rules and facility capacities
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        cleaning = config["cleaning"]
        trip_limit = int(cleaning["after_every_n_trips"])
        duration = timedelta(hours=float(cleaning["default_duration_hours"]))
        capacities = {
            city: int(details["capacity_trains"])
            for city, details in cleaning["facilities"].items()
        }
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        fail(f"cannot read required configuration: {error}")
    if trip_limit < 1 or duration <= timedelta() or any(capacity < 1 for capacity in capacities.values()):
        fail("cleaning has invalid values")

    train_rows = read_csv(args.trains, {"train_id", "initial_location", "is_hot_reserve"})
    trains: dict[str, dict[str, object]] = {}
    for row in train_rows:
        train_id = row["train_id"]
        if not train_id or train_id in trains:
            fail(f"duplicate or empty train id: {train_id!r}")
        trains[train_id] = {
            "initial_location": row["initial_location"],
            "is_hot_reserve": as_bool(row["is_hot_reserve"], "is_hot_reserve"),
        }

    trip_rows = read_csv(args.trips, {
        "trip_id", "train_id", "departure_time", "arrival_time", "origin", "destination",
        "requires_cleaning_after"
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
            "train_id": row["train_id"], "origin": row["origin"], "destination": row["destination"],
            "departure": departure, "arrival": arrival,
            "requires_cleaning_after": as_bool(row["requires_cleaning_after"], "requires_cleaning_after"),
            "is_real": True,
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

    maintenance_rows = read_csv(args.maintenance_schedule, {
        "train_id", "scheduled_start", "scheduled_end", "status"
    })
    wheel_rows = read_csv(args.wheel_schedule, {
        "train_id", "scheduled_start", "scheduled_end", "status"
    })

    scenario_start = min(trip["departure"] for trip in real_trips).replace(
        hour=0, minute=0, second=0, microsecond=0
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
    forecast_end = scenario_start.date() + timedelta(days=args.forecast_days - 1)
    day = last_real_day + timedelta(days=1)
    # add one extra day for the last cleaning deadline
    while day <= forecast_end + timedelta(days=1):
        before = {train_id: len(items) for train_id, items in trips_by_train.items()}
        run_template_day(day, template, states, trips_by_train, active_ids)
        for train_id, items in trips_by_train.items():
            for trip in items[before.get(train_id, 0):]:
                trip["is_real"] = False
                trip["requires_cleaning_after"] = False
        day += timedelta(days=1)
    for train_trips in trips_by_train.values():
        train_trips.sort(key=lambda item: item["departure"])

    cleaning_jobs: list[dict[str, object]] = []
    for train_id, train_trips in trips_by_train.items():
        trips_since_cleaning = 0
        for index, trip in enumerate(train_trips[:-1]):
            if trip["arrival"].date() > forecast_end:
                break
            trips_since_cleaning += 1
            required = trip["requires_cleaning_after"] if trip["is_real"] else trips_since_cleaning == trip_limit
            if not required:
                continue
            next_trip = train_trips[index + 1]
            if trip["destination"] not in capacities or next_trip["origin"] != trip["destination"]:
                fail(f"trip sequence for {train_id}: no cleaning facility at turnaround")
            cleaning_jobs.append({
                "train_id": train_id, "location": trip["destination"],
                "earliest": trip["arrival"], "latest": next_trip["departure"],
                "source_trip_end": trip["arrival"],
            })
            trips_since_cleaning = 0

    job_rows: list[dict[str, object]] = []
    for number, job in enumerate(sorted(cleaning_jobs, key=lambda item: (item["earliest"], item["train_id"])), start=1):
        job_rows.append({
            "cleaning_job_id": f"CLEAN-{number:06d}", "train_id": job["train_id"],
            "location": job["location"],
            "earliest_allowed_at": job["earliest"].isoformat(timespec="minutes"),
            "latest_allowed_at": job["latest"].isoformat(timespec="minutes"),
            "duration_hours": duration.total_seconds() / 3600,
        })
    write_csv(args.jobs_output, [
        "cleaning_job_id", "train_id", "location", "earliest_allowed_at",
        "latest_allowed_at", "duration_hours"
    ], job_rows)

    train_busy: dict[str, list[TimeSlot]] = defaultdict(list)
    for row in maintenance_rows + wheel_rows:
        if row["status"] != "scheduled":
            continue
        start = parse_datetime(row["scheduled_start"], "scheduled_start")
        end = parse_datetime(row["scheduled_end"], "scheduled_end")
        train_busy[row["train_id"]].append(TimeSlot(start, end))
    for slots in train_busy.values():
        slots.sort(key=lambda item: item.start)
    facility_bookings: dict[str, list[FacilityBooking]] = defaultdict(list)
    schedule_rows: list[dict[str, object]] = []

    # place the shortest and earliest cleaning jobs first
    for number, job in enumerate(sorted(job_rows, key=lambda item: (
        item["latest_allowed_at"], item["earliest_allowed_at"], item["train_id"]
    )), start=1):
        earliest = parse_datetime(job["earliest_allowed_at"], "earliest_allowed_at")
        latest = parse_datetime(job["latest_allowed_at"], "latest_allowed_at")
        window = TimeSlot(earliest, latest)
        city_bookings = facility_bookings[job["location"]]
        slot = find_cleaning_slot(
            window, train_busy[job["train_id"]], duration, city_bookings, capacities[job["location"]]
        )
        status, reason = "scheduled", ""
        if slot is None:
            train_slot = find_free_slot([window], train_busy[job["train_id"]], earliest, latest, duration)
            reason = "maintenance_or_wheel_overlap" if train_slot is None else "no_cleaning_capacity_before_departure"
            status = "unscheduled"
        else:
            train_busy[job["train_id"]].append(slot)
            train_busy[job["train_id"]].sort(key=lambda item: item.start)
            booking = FacilityBooking(slot.start, slot.end)
            index = bisect_left(city_bookings, booking.start, key=lambda item: item.start)
            city_bookings.insert(index, booking)
        schedule_rows.append({
            "cleaning_schedule_id": f"CSCH-{number:06d}", "cleaning_job_id": job["cleaning_job_id"],
            "train_id": job["train_id"], "location": job["location"],
            "scheduled_start": "" if slot is None else slot.start.isoformat(timespec="minutes"),
            "scheduled_end": "" if slot is None else slot.end.isoformat(timespec="minutes"),
            "duration_hours": job["duration_hours"], "status": status, "reason": reason,
            "earliest_allowed_at": job["earliest_allowed_at"],
            "latest_allowed_at": job["latest_allowed_at"],
        })
    write_csv(args.schedule_output, [
        "cleaning_schedule_id", "cleaning_job_id", "train_id", "location", "scheduled_start",
        "scheduled_end", "duration_hours", "status", "reason", "earliest_allowed_at",
        "latest_allowed_at"
    ], schedule_rows)
    facility_load = load_rows(facility_bookings)
    write_csv(args.load_output, ["location", "start", "end", "trains_cleaning"], facility_load)
    summary_rows = []
    for scope, rows in [("all", schedule_rows)] + [
        (city, [row for row in schedule_rows if row["location"] == city])
        for city in sorted(capacities)
    ]:
        peak = max(
            (int(row["trains_cleaning"]) for row in facility_load
             if scope == "all" or row["location"] == scope),
            default=0,
        )
        summary_rows.append({
            "scope": scope, "total_jobs": len(rows),
            "scheduled_jobs": sum(row["status"] == "scheduled" for row in rows),
            "unscheduled_jobs": sum(row["status"] == "unscheduled" for row in rows),
            "maintenance_overlap_conflicts": sum(row["reason"] == "maintenance_or_wheel_overlap" for row in rows),
            "facility_capacity_conflicts": sum(row["reason"] == "no_cleaning_capacity_before_departure" for row in rows),
            "peak_trains_cleaning": peak,
        })
    write_csv(args.summary_output, [
        "scope", "total_jobs", "scheduled_jobs", "unscheduled_jobs",
        "maintenance_overlap_conflicts", "facility_capacity_conflicts", "peak_trains_cleaning"
    ], summary_rows)
    print(f"created {args.jobs_output}: {len(job_rows)} cleaning jobs")
    print(f"created {args.schedule_output}: {len(schedule_rows)} cleaning rows")
    print(f"created {args.summary_output}: {len(summary_rows)} summary rows")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
