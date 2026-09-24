#!/usr/bin/env python3
"""build mileage history and repeated maintenance jobs."""

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
    mileage: int
    is_hot_reserve: bool
    total_trips: int = 0
    daily_trips: int = 0


@dataclass
class MileagePoint:
    at: datetime
    mileage: int


@dataclass
class Candidate:
    train_id: str
    cycle: dict
    target: int
    lower: int
    upper: int
    earliest: datetime
    is_hot_reserve: bool


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
    # write rows with the chosen column order
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def first_crossing(points: list[MileagePoint], limit: int) -> datetime | None:
    # find when mileage first reaches one limit
    for point in points:
        if point.mileage >= limit:
            return point.at
    return None


def run_template_day(
    day: date, template: list[dict[str, object]], states: dict[str, TrainState],
    points: dict[str, list[MileagePoint]], active_ids: list[str],
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
        # move the selected train to its next place
        state = states[train_id]
        state.location = trip["destination"]
        state.available_at = arrival
        state.mileage += trip["distance_km"]
        state.daily_trips += 1
        state.total_trips += 1
        points[train_id].append(MileagePoint(arrival, state.mileage))


def build_candidates(
    trains: dict[str, dict[str, object]], points: dict[str, list[MileagePoint]],
    cycles: list[dict], scenario_start: datetime,
) -> list[Candidate]:
    # create every cycle window opened in the planning horizon
    candidates: list[Candidate] = []
    for train_id, train in trains.items():
        initial = train["initial_mileage_km"]
        horizon_mileage = points[train_id][-1].mileage
        for cycle in cycles:
            interval = cycle["interval_km"]
            tolerance = round(interval * cycle["tolerance_fraction"])
            target = (initial // interval + 1) * interval
            while target - tolerance <= horizon_mileage:
                lower, upper = target - tolerance, target + tolerance
                earliest = scenario_start if initial >= lower else first_crossing(
                    points[train_id], lower
                )
                if earliest is not None:
                    candidates.append(Candidate(
                        train_id, cycle, target, lower, upper, earliest,
                        train["is_hot_reserve"]
                    ))
                target += interval
    # keep only the higher cycle for one target
    selected: dict[tuple[str, int], Candidate] = {}
    for candidate in candidates:
        key = (candidate.train_id, candidate.target)
        previous = selected.get(key)
        if previous is None or candidate.cycle["interval_km"] > previous.cycle["interval_km"]:
            selected[key] = candidate
    return sorted(selected.values(), key=lambda item: (item.train_id, item.target, item.cycle["code"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/case_config.json"))
    parser.add_argument("--trains", type=Path, default=Path("data/trains.csv"))
    parser.add_argument("--trips", type=Path, default=Path("data/trips.csv"))
    parser.add_argument("--template", type=Path, default=Path("data/trips_template.csv"))
    parser.add_argument("--forecast-output", type=Path, default=Path("data/mileage_forecast.csv"))
    parser.add_argument("--jobs-output", type=Path, default=Path("data/maintenance_jobs.csv"))
    parser.add_argument("--forecast-days", type=int, default=730)
    args = parser.parse_args()
    if args.forecast_days < 1:
        fail("forecast-days must be at least 1")

    # load the cycles and the depot location
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        cycles = config["maintenance_cycles"]
        depot = config["route"]["depot_location"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        fail(f"cannot read required configuration: {error}")
    if not isinstance(cycles, list) or not cycles or not depot:
        fail("config has no maintenance cycles or depot location")
    for cycle in cycles:
        required = {"code", "interval_km", "tolerance_fraction", "downtime_hours", "splittable"}
        if not required <= cycle.keys() or cycle["interval_km"] <= 0:
            fail(f"invalid maintenance cycle: {cycle}")

    # read the start state of every train
    train_rows = read_csv(args.trains, {
        "train_id", "is_hot_reserve", "initial_mileage_km", "initial_location"
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
        if mileage < 0 or not row["initial_location"]:
            fail(f"train {train_id}: invalid initial state")
        trains[train_id] = {
            "initial_mileage_km": mileage,
            "initial_location": row["initial_location"],
            "is_hot_reserve": as_bool(row["is_hot_reserve"], "is_hot_reserve"),
        }

    # read real trips and calculate their mileage
    trip_rows = read_csv(args.trips, {
        "trip_id", "train_id", "service_date", "departure_time", "arrival_time",
        "origin", "destination", "distance_km"
    })
    trips: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for row in trip_rows:
        if not row["trip_id"] or row["trip_id"] in seen_ids or row["train_id"] not in trains:
            fail(f"invalid trip id or train: {row['trip_id']!r}")
        seen_ids.add(row["trip_id"])
        departure = parse_datetime(row["departure_time"], "departure_time")
        arrival = parse_datetime(row["arrival_time"], "arrival_time")
        if arrival <= departure:
            fail(f"trip {row['trip_id']}: arrival must be after departure")
        try:
            distance = int(row["distance_km"])
        except ValueError:
            fail(f"trip {row['trip_id']}: invalid distance")
        if distance <= 0 or not row["origin"] or not row["destination"]:
            fail(f"trip {row['trip_id']}: invalid route")
        trips.append({**row, "departure": departure, "arrival": arrival, "distance_km": distance})
    if not trips:
        fail("trips.csv must contain at least one trip")

    trips_by_train: dict[str, list[dict[str, object]]] = defaultdict(list)
    for trip in sorted(trips, key=lambda item: (item["departure"], item["trip_id"])):
        trips_by_train[trip["train_id"]].append(trip)
    for train_id, train_trips in trips_by_train.items():
        mileage = trains[train_id]["initial_mileage_km"]
        for trip in train_trips:
            trip["mileage_before_km"] = mileage
            mileage += trip["distance_km"]
            trip["mileage_after_km"] = mileage
    write_csv(args.forecast_output, [
        "trip_id", "train_id", "service_date", "departure_time", "arrival_time", "origin",
        "destination", "distance_km", "mileage_before_km", "mileage_after_km"
    ], [{
        key: trip[key] for key in (
            "trip_id", "train_id", "service_date", "departure_time", "arrival_time", "origin",
            "destination", "distance_km", "mileage_before_km", "mileage_after_km"
        )
    } for trip in trips])

    # load one reusable day of trips
    template_rows = read_csv(args.template, {
        "template_trip_id", "departure_time", "arrival_time", "origin", "destination", "distance_km"
    })
    template: list[dict[str, object]] = []
    for row in template_rows:
        try:
            distance = int(row["distance_km"])
        except ValueError:
            fail(f"template trip {row['template_trip_id']}: invalid distance")
        if not row["template_trip_id"] or distance <= 0:
            fail("template trip has an empty id or invalid distance")
        template.append({
            **row, "distance_km": distance,
            "departure_clock": parse_time(row["departure_time"], "template departure time"),
            "arrival_clock": parse_time(row["arrival_time"], "template arrival time"),
        })
    if not template:
        fail("trips_template.csv must contain at least one trip")
    template.sort(key=lambda item: (item["departure_clock"], item["template_trip_id"]))

    scenario_start = min(trip["departure"] for trip in trips).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    states = {
        train_id: TrainState(train["initial_location"], scenario_start, train["initial_mileage_km"], train["is_hot_reserve"])
        for train_id, train in trains.items()
    }
    points = {
        train_id: [MileagePoint(scenario_start, train["initial_mileage_km"])]
        for train_id, train in trains.items()
    }
    # apply real trips before the repeated template
    for trip in sorted(trips, key=lambda item: (item["departure"], item["trip_id"])):
        state = states[trip["train_id"]]
        if state.location != trip["origin"] or state.available_at > trip["departure"]:
            fail(f"trip {trip['trip_id']}: train state does not match its route")
        state.location, state.available_at = trip["destination"], trip["arrival"]
        state.mileage, state.total_trips = trip["mileage_after_km"], state.total_trips + 1
        points[trip["train_id"]].append(MileagePoint(trip["arrival"], state.mileage))

    active_ids = sorted(train_id for train_id, state in states.items() if not state.is_hot_reserve)
    if not active_ids:
        fail("no active trains available for the extended forecast")
    last_actual_day = max(trip["departure"].date() for trip in trips)
    planning_end = max(last_actual_day, scenario_start.date() + timedelta(days=args.forecast_days - 1))
    day = last_actual_day + timedelta(days=1)
    # run the template through the maintenance horizon
    while day <= planning_end:
        run_template_day(day, template, states, points, active_ids)
        day += timedelta(days=1)

    candidates = build_candidates(trains, points, cycles, scenario_start)
    required: dict[str, int] = {}
    for candidate in candidates:
        if not candidate.is_hot_reserve:
            required[candidate.train_id] = max(required.get(candidate.train_id, 0), candidate.upper)
    # continue only until each open job gets a deadline
    while any(states[train_id].mileage < limit for train_id, limit in required.items()):
        run_template_day(day, template, states, points, active_ids)
        day += timedelta(days=1)

    fields = [
        "job_id", "train_id", "cycle_code", "target_mileage_km", "lower_bound_km",
        "upper_bound_km", "earliest_allowed_at", "latest_allowed_at", "downtime_hours",
        "splittable", "location", "status", "blocking_reason"
    ]
    jobs: list[dict[str, object]] = []
    for number, candidate in enumerate(candidates, start=1):
        deadline = None if candidate.is_hot_reserve else first_crossing(points[candidate.train_id], candidate.upper)
        if deadline is None and not candidate.is_hot_reserve:
            fail(f"job for {candidate.train_id} has no deadline")
        jobs.append({
            "job_id": f"JOB-{number:06d}", "train_id": candidate.train_id,
            "cycle_code": candidate.cycle["code"], "target_mileage_km": candidate.target,
            "lower_bound_km": candidate.lower, "upper_bound_km": candidate.upper,
            "earliest_allowed_at": candidate.earliest.isoformat(timespec="minutes"),
            "latest_allowed_at": "" if deadline is None else deadline.isoformat(timespec="minutes"),
            "downtime_hours": candidate.cycle["downtime_hours"],
            "splittable": str(candidate.cycle["splittable"]).lower(), "location": depot,
            "status": "blocked_by_hot_reserve" if candidate.is_hot_reserve else "pending",
            "blocking_reason": "hot_reserve" if candidate.is_hot_reserve else "",
        })
    write_csv(args.jobs_output, fields, jobs)
    print(f"created {args.forecast_output}: {len(trips)} real trip rows")
    print(f"created {args.jobs_output}: {len(jobs)} maintenance jobs")
    print(f"maintenance planning horizon: {planning_end.isoformat()}")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
