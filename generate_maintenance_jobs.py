"""build mileage history and maintenance windows"""

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


def read_csv(path: Path, required_fields: set[str]) -> list[dict[str, str]]:
    # read rows and check required columns
    try:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                fail(f"{path}: missing CSV header")
            missing = required_fields - set(reader.fieldnames)
            if missing:
                fail(f"{path}: missing fields: {', '.join(sorted(missing))}")
            return list(reader)
    except OSError as error:
        fail(f"Cannot read {path}: {error}")


def parse_datetime(value: str, field: str) -> datetime:
    # read one date and time value
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        fail(f"Invalid {field} datetime {value!r}: {error}")


def parse_time(value: str, field: str) -> time:
    # read one time without a date
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        fail(f"Invalid {field} time {value!r}: {error}")


def as_bool(value: str, field: str) -> bool:
    # accept only true or false values
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    fail(f"Invalid {field} boolean {value!r}")


def load_config(path: Path) -> dict:
    # load only data needed for this step
    try:
        with path.open(encoding="utf-8") as source:
            config = json.load(source)
        cycles = config["maintenance_cycles"]
        depot = config["route"]["depot_location"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as error:
        fail(f"Cannot read required configuration from {path}: {error}")
    if not isinstance(cycles, list) or not cycles:
        fail("maintenance_cycles must be a non-empty list")
    # check every service cycle before use
    for cycle in cycles:
        required = {
            "code", "interval_km", "tolerance_fraction", "downtime_hours", "splittable"
        }
        if not required <= cycle.keys():
            fail(f"Invalid maintenance cycle: required {sorted(required)}")
        if cycle["interval_km"] <= 0 or cycle["tolerance_fraction"] < 0:
            fail(f"Invalid interval or tolerance for cycle {cycle['code']}")
    if not depot:
        fail("route.depot_location must not be empty")
    return config


def trip_timestamps(row: dict[str, str]) -> tuple[datetime, datetime]:
    # make sure the trip has positive time
    departure = parse_datetime(row["departure_time"], "departure_time")
    arrival = parse_datetime(row["arrival_time"], "arrival_time")
    if arrival <= departure:
        fail(f"Trip {row['trip_id']}: arrival must be after departure")
    return departure, arrival


def write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    # write a csv file with the chosen column order
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build_candidates(
    trains: dict[str, dict[str, object]],
    trips_by_train: dict[str, list[dict[str, object]]],
    cycles: list[dict],
    scenario_start: datetime,
) -> list[Candidate]:
    # find the next service window for each train
    candidates: list[Candidate] = []
    for train_id, train in trains.items():
        initial_mileage = train["initial_mileage_km"]
        for cycle in cycles:
            interval = cycle["interval_km"]
            # take the first full interval after current mileage
            target = (initial_mileage // interval + 1) * interval
            tolerance = round(interval * cycle["tolerance_fraction"])
            lower, upper = target - tolerance, target + tolerance
            earliest = scenario_start if initial_mileage >= lower else None
            mileage = initial_mileage
            # find when the lower limit is first reached
            for trip in trips_by_train[train_id]:
                mileage = trip["mileage_after_km"]
                if earliest is None and mileage >= lower:
                    earliest = trip["arrival"]
            if earliest is not None:
                candidates.append(Candidate(
                    train_id, cycle, target, lower, upper, earliest, train["is_hot_reserve"]
                ))
    # keep only the higher cycle for one target
    selected: dict[tuple[str, int], Candidate] = {}
    for candidate in candidates:
        key = (candidate.train_id, candidate.target)
        previous = selected.get(key)
        if previous is None or candidate.cycle["interval_km"] > previous.cycle["interval_km"]:
            selected[key] = candidate
    return sorted(selected.values(), key=lambda item: (item.train_id, item.target, item.cycle["code"]))


def apply_actual_trips(
    trips: list[dict[str, object]], states: dict[str, TrainState]
) -> None:
    # update train places after real trips
    for trip in sorted(trips, key=lambda row: (row["departure"], row["trip_id"])):
        state = states[trip["train_id"]]
        if state.location != trip["origin"]:
            fail(
                f"Trip {trip['trip_id']}: {trip['train_id']} is at {state.location}, "
                f"not {trip['origin']}"
            )
        if state.available_at > trip["departure"]:
            fail(f"Trip {trip['trip_id']}: {trip['train_id']} is not available at departure")
        # move the train to the trip destination
        state.location = trip["destination"]
        state.available_at = trip["arrival"]
        state.mileage = trip["mileage_after_km"]
        state.total_trips += 1


def first_actual_deadlines(
    candidates: list[Candidate], trips_by_train: dict[str, list[dict[str, object]]]
) -> dict[tuple[str, int], datetime]:
    # use a real arrival when it reaches the upper limit
    deadlines: dict[tuple[str, int], datetime] = {}
    for candidate in candidates:
        if candidate.is_hot_reserve:
            continue
        for trip in trips_by_train[candidate.train_id]:
            if trip["mileage_after_km"] >= candidate.upper:
                deadlines[(candidate.train_id, candidate.target)] = trip["arrival"]
                break
    return deadlines


def extend_until_deadlines(
    candidates: list[Candidate], states: dict[str, TrainState], template: list[dict[str, object]],
    first_extension_day: date, deadlines: dict[tuple[str, int], datetime]
) -> None:
    # run future template trips only in memory
    pending = {
        (item.train_id, item.target): item
        for item in candidates
        if not item.is_hot_reserve and (item.train_id, item.target) not in deadlines
    }
    active_ids = sorted(
        train_id for train_id, state in states.items() if not state.is_hot_reserve
    )
    if not active_ids:
        fail("No active trains available for the extended forecast")
    day = first_extension_day
    while pending:
        # reset the daily trip counter
        for state in states.values():
            state.daily_trips = 0
        for trip in template:
            # put the template time on the current day
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
                fail(
                    f"No active train can operate template trip {trip['template_trip_id']} "
                    f"at {departure.isoformat(timespec='minutes')}"
                )
            train_id = min(eligible, key=lambda item: (
                states[item].daily_trips, states[item].total_trips,
                states[item].available_at, item
            ))
            # prefer a train with less work today
            state = states[train_id]
            state.location = trip["destination"]
            state.available_at = arrival
            state.mileage += trip["distance_km"]
            state.daily_trips += 1
            state.total_trips += 1
            # close jobs when the upper limit is reached
            for key, candidate in list(pending.items()):
                if candidate.train_id == train_id and state.mileage >= candidate.upper:
                    deadlines[key] = arrival
                    del pending[key]
        day += timedelta(days=1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/case_config.json"))
    parser.add_argument("--trains", type=Path, default=Path("data/trains.csv"))
    parser.add_argument("--trips", type=Path, default=Path("data/trips.csv"))
    parser.add_argument("--template", type=Path, default=Path("data/trips_template.csv"))
    parser.add_argument("--forecast-output", type=Path, default=Path("data/mileage_forecast.csv"))
    parser.add_argument("--jobs-output", type=Path, default=Path("data/maintenance_jobs.csv"))
    args = parser.parse_args()

    # load trains and their starting mileage
    config = load_config(args.config)
    # read the start state of every train
    train_rows = read_csv(args.trains, {
        "train_id", "is_hot_reserve", "initial_mileage_km", "initial_location"
    })
    trains: dict[str, dict[str, object]] = {}
    for row in train_rows:
        train_id = row["train_id"]
        if not train_id or train_id in trains:
            fail(f"Duplicate or empty train_id in {args.trains}: {train_id!r}")
        try:
            initial_mileage = int(row["initial_mileage_km"])
        except ValueError:
            fail(f"Train {train_id}: invalid initial_mileage_km {row['initial_mileage_km']!r}")
        if initial_mileage < 0 or not row["initial_location"]:
            fail(f"Train {train_id}: invalid initial state")
        trains[train_id] = {
            "initial_mileage_km": initial_mileage,
            "initial_location": row["initial_location"],
            "is_hot_reserve": as_bool(row["is_hot_reserve"], "is_hot_reserve"),
        }

    trip_rows = read_csv(args.trips, {
        "trip_id", "train_id", "service_date", "departure_time", "arrival_time",
        "origin", "destination", "distance_km"
    })
    trips: list[dict[str, object]] = []
    seen_trip_ids: set[str] = set()
    # validate every real trip before calculation
    for row in trip_rows:
        if not row["trip_id"] or row["trip_id"] in seen_trip_ids:
            fail(f"Duplicate or empty trip_id in {args.trips}: {row['trip_id']!r}")
        seen_trip_ids.add(row["trip_id"])
        if row["train_id"] not in trains:
            fail(f"Trip {row['trip_id']}: unknown train {row['train_id']}")
        departure, arrival = trip_timestamps(row)
        try:
            distance = int(row["distance_km"])
        except ValueError:
            fail(f"Trip {row['trip_id']}: invalid distance_km {row['distance_km']!r}")
        if distance <= 0 or not row["origin"] or not row["destination"]:
            fail(f"Trip {row['trip_id']}: invalid route")
        trips.append({**row, "departure": departure, "arrival": arrival, "distance_km": distance})
    if not trips:
        fail("trips.csv must contain at least one trip")

    trips_by_train: dict[str, list[dict[str, object]]] = defaultdict(list)
    # group trips so mileage can be counted per train
    for trip in sorted(trips, key=lambda row: (row["departure"], row["trip_id"])):
        trips_by_train[trip["train_id"]].append(trip)
    for train_id in trains:
        # add real trip distance to train mileage
        mileage = trains[train_id]["initial_mileage_km"]
        for trip in trips_by_train[train_id]:
            trip["mileage_before_km"] = mileage
            mileage += trip["distance_km"]
            trip["mileage_after_km"] = mileage

    forecast_fields = [
        "trip_id", "train_id", "service_date", "departure_time", "arrival_time", "origin",
        "destination", "distance_km", "mileage_before_km", "mileage_after_km"
    ]
    write_csv(args.forecast_output, forecast_fields, [
        {field: trip[field] for field in forecast_fields} for trip in trips
    ])

    # create service jobs from the real schedule
    scenario_start = min(trip["departure"] for trip in trips).replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = build_candidates(trains, trips_by_train, config["maintenance_cycles"], scenario_start)
    states: dict[str, TrainState] = {}
    for train_id, train in trains.items():
        states[train_id] = TrainState(
            train["initial_location"], scenario_start, train["initial_mileage_km"],
            train["is_hot_reserve"]
        )
    apply_actual_trips(trips, states)
    # use real dates when a deadline is already reached
    deadlines = first_actual_deadlines(candidates, trips_by_train)

    # load one reusable day of trips
    template_rows = read_csv(args.template, {
        "template_trip_id", "departure_time", "arrival_time", "origin", "destination", "distance_km"
    })
    template: list[dict[str, object]] = []
    for row in template_rows:
        try:
            distance = int(row["distance_km"])
        except ValueError:
            fail(f"Template trip {row['template_trip_id']}: invalid distance_km")
        if not row["template_trip_id"] or distance <= 0:
            fail("Template trip has an empty id or non-positive distance")
        template.append({
            **row, "distance_km": distance,
            "departure_clock": parse_time(row["departure_time"], "template departure_time"),
            "arrival_clock": parse_time(row["arrival_time"], "template arrival_time"),
        })
    if not template:
        fail("trips_template.csv must contain at least one trip")
    template.sort(key=lambda row: (row["departure_clock"], row["template_trip_id"]))
    # continue after the supplied schedule ends
    last_service_day = max(parse_datetime(row["departure_time"], "departure_time").date() for row in trips)
    extend_until_deadlines(candidates, states, template, last_service_day + timedelta(days=1), deadlines)

    job_fields = [
        "job_id", "train_id", "cycle_code", "target_mileage_km", "lower_bound_km",
        "upper_bound_km", "earliest_allowed_at", "latest_allowed_at", "downtime_hours",
        "splittable", "location", "status", "blocking_reason"
    ]
    jobs: list[dict[str, object]] = []
    # make final rows for the maintenance planner
    for number, candidate in enumerate(candidates, start=1):
        blocked = candidate.is_hot_reserve
        deadline = "" if blocked else deadlines[(candidate.train_id, candidate.target)].isoformat(timespec="minutes")
        jobs.append({
            "job_id": f"JOB-{number:06d}", "train_id": candidate.train_id,
            "cycle_code": candidate.cycle["code"], "target_mileage_km": candidate.target,
            "lower_bound_km": candidate.lower, "upper_bound_km": candidate.upper,
            "earliest_allowed_at": candidate.earliest.isoformat(timespec="minutes"),
            "latest_allowed_at": deadline, "downtime_hours": candidate.cycle["downtime_hours"],
            "splittable": str(candidate.cycle["splittable"]).lower(),
            "location": config["route"]["depot_location"],
            "status": "blocked_by_hot_reserve" if blocked else "pending",
            "blocking_reason": "hot_reserve" if blocked else "",
        })
    write_csv(args.jobs_output, job_fields, jobs)
    print(f"Created {args.forecast_output}: {len(trips)} trip rows")
    print(f"Created {args.jobs_output}: {len(jobs)} maintenance jobs")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
