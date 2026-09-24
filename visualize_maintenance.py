#!/usr/bin/env python3
"""make a simple html view for maintenance jobs."""

from __future__ import annotations

import argparse
import csv
import html
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


CYCLE_COLORS = {
    "IS100": "#38bdf8",
    "IS200": "#22c55e",
    "IS510": "#a78bfa",
    "IS520": "#f59e0b",
    "IS530": "#f97316",
    "IS540": "#ef4444",
    "IS600": "#ec4899",
    "IS700": "#64748b",
}


def fail(message: str) -> None:
    # stop with a clear data error
    raise ValueError(message)


def parse_datetime(value: str, field: str) -> datetime:
    # read one date and time value
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        fail(f"invalid {field}: {value!r} ({error})")


def read_jobs(path: Path) -> list[dict[str, object]]:
    # read jobs and check main columns
    required = {
        "job_id", "train_id", "cycle_code", "target_mileage_km", "lower_bound_km",
        "upper_bound_km", "earliest_allowed_at", "latest_allowed_at", "downtime_hours",
        "location", "status", "blocking_reason",
    }
    try:
        with path.open(newline="", encoding="utf-8") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames is None:
                fail(f"{path}: missing csv header")
            missing = required - set(reader.fieldnames)
            if missing:
                fail(f"{path}: missing fields: {', '.join(sorted(missing))}")
            rows = list(reader)
    except OSError as error:
        fail(f"cannot read {path}: {error}")
    if not rows:
        fail(f"{path}: no maintenance jobs")

    jobs: list[dict[str, object]] = []
    for row in rows:
        if not row["job_id"] or not row["train_id"]:
            fail("job has an empty id or train id")
        earliest = parse_datetime(row["earliest_allowed_at"], "earliest_allowed_at")
        latest = None if not row["latest_allowed_at"] else parse_datetime(
            row["latest_allowed_at"], "latest_allowed_at"
        )
        if latest is not None and latest < earliest:
            fail(f"job {row['job_id']}: latest time is before earliest time")
        jobs.append({**row, "earliest": earliest, "latest": latest})
    return jobs


def date_label(value: datetime | None) -> str:
    # show dates in a short local form
    return "—" if value is None else value.strftime("%d.%m.%Y %H:%M")


def timeline_bar(job: dict[str, object], start: datetime, end: datetime) -> str:
    # turn one job window into a colored bar
    latest = job["latest"] or end
    total_seconds = max((end - start).total_seconds(), 1)
    left = (job["earliest"] - start).total_seconds() / total_seconds * 100
    width = max((latest - job["earliest"]).total_seconds() / total_seconds * 100, 0.8)
    color = "#dc2626" if job["status"] != "pending" else CYCLE_COLORS.get(job["cycle_code"], "#334155")
    title = html.escape(
        f"{job['cycle_code']}: {date_label(job['earliest'])} — {date_label(job['latest'])}"
    )
    return (
        f'<span class="bar" title="{title}" style="left:{left:.2f}%;width:{width:.2f}%;'
        f'background:{color}">{html.escape(str(job["cycle_code"]))}</span>'
    )


def build_html(jobs: list[dict[str, object]]) -> str:
    # prepare data for the browser filter
    start = min(job["earliest"] for job in jobs)
    end = max((job["latest"] or job["earliest"]) for job in jobs)
    page_jobs = [{
        "job_id": job["job_id"], "train_id": job["train_id"],
        "cycle_code": job["cycle_code"], "earliest": job["earliest"].isoformat(),
        "latest": "" if job["latest"] is None else job["latest"].isoformat(),
        "downtime_hours": job["downtime_hours"], "status": job["status"],
    } for job in jobs]
    job_data = json.dumps(page_jobs, ensure_ascii=False).replace("</", "<\\/")

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Maintenance jobs</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #f1f5f9; color: #0f172a; font: 14px/1.4 Arial, sans-serif; }}
    main {{ max-width: 1400px; margin: 0 auto; padding: 28px; }}
    h1 {{ margin: 0 0 4px; font-size: 28px; }}
    h2 {{ margin: 28px 0 12px; font-size: 20px; }}
    .sub {{ margin: 0; color: #475569; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, minmax(150px, 1fr)); gap: 12px; margin-top: 20px; }}
    .card, .panel {{ background: white; border-radius: 10px; box-shadow: 0 1px 3px #cbd5e1; }}
    .card {{ padding: 16px; }}
    .card small {{ display: block; color: #64748b; }}
    .card strong {{ display: block; margin-top: 5px; font-size: 20px; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: end; margin-top: 20px; }}
    label {{ display: grid; gap: 4px; color: #475569; font-size: 12px; }}
    input, select {{ min-height: 34px; padding: 5px 8px; border: 1px solid #cbd5e1; border-radius: 6px; background: white; color: #0f172a; }}
    .panel {{ padding: 16px; overflow-x: auto; }}
    .timeline-row {{ display: grid; grid-template-columns: 112px minmax(700px, 1fr); min-height: 32px; border-bottom: 1px solid #e2e8f0; }}
    .train {{ padding: 7px 8px 7px 0; font-family: monospace; }}
    .track {{ position: relative; min-height: 32px; background: repeating-linear-gradient(90deg, #f8fafc 0, #f8fafc calc(10% - 1px), #e2e8f0 calc(10% - 1px), #e2e8f0 10%); }}
    .bar {{ position: absolute; top: 6px; height: 20px; min-width: 7px; padding: 2px 5px; overflow: hidden; border-radius: 4px; color: white; font-size: 11px; white-space: nowrap; }}
    .range {{ display: flex; justify-content: space-between; margin-left: 112px; color: #64748b; font-size: 12px; }}
    .legend {{ display: inline-flex; align-items: center; gap: 5px; margin: 0 14px 8px 0; }}
    .legend i {{ width: 12px; height: 12px; border-radius: 2px; }}
    table {{ width: 100%; border-collapse: collapse; white-space: nowrap; }}
    th, td {{ padding: 9px; text-align: left; border-bottom: 1px solid #e2e8f0; }}
    th {{ color: #475569; background: #f8fafc; }}
    .badge {{ padding: 2px 6px; border-radius: 4px; background: #e2e8f0; font-family: monospace; }}
    @media (max-width: 700px) {{ main {{ padding: 16px; }} .cards {{ grid-template-columns: repeat(2, 1fr); }} }}
  </style>
</head>
<body>
  <main>
    <h1>Maintenance jobs</h1>
    <p class="sub">maintenance windows from the current forecast</p>
    <section class="controls">
      <label>start month<input id="start-month" type="month"></label>
      <label>period<select id="period">
        <option value="1">1 month</option>
        <option value="3">3 months</option>
        <option value="6">6 months</option>
        <option value="12">12 months</option>
        <option value="24">24 months</option>
        <option value="all">all data</option>
      </select></label>
    </section>
    <section class="cards">
      <div class="card"><small>jobs</small><strong id="job-count">—</strong></div>
      <div class="card"><small>trains with jobs</small><strong id="train-count">—</strong></div>
      <div class="card"><small>nearest deadline</small><strong id="nearest">—</strong></div>
      <div class="card"><small>last deadline</small><strong id="last">—</strong></div>
    </section>
    <h2>Timeline</h2>
    <div id="legend"></div>
    <section class="panel">
      <div class="range"><span id="range-start"></span><span id="range-end"></span></div>
      <div id="timeline"></div>
    </section>
    <h2>Jobs</h2>
    <section class="panel">
      <table>
        <thead><tr><th>job</th><th>train</th><th>cycle</th><th>window opens</th><th>deadline</th><th>work</th><th>status</th></tr></thead>
        <tbody id="jobs-table"></tbody>
      </table>
    </section>
  </main>
  <script id="jobs-data" type="application/json">{job_data}</script>
  <script>
    const jobs = JSON.parse(document.getElementById('jobs-data').textContent);
    const colors = {json.dumps(CYCLE_COLORS)};
    const fullStart = new Date('{start.isoformat()}');
    const fullEnd = new Date('{end.isoformat()}');
    const monthInput = document.getElementById('start-month');
    const periodInput = document.getElementById('period');
    monthInput.value = fullStart.toISOString().slice(0, 7);

    function text(value) {{
      return value ? new Intl.DateTimeFormat('ru-RU', {{
        day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit'
      }}).format(value) : '—';
    }}

    function escapeText(value) {{
      const node = document.createElement('span');
      node.textContent = value;
      return node.innerHTML;
    }}

    function selectedRange() {{
      const start = new Date(monthInput.value + '-01T00:00');
      if (periodInput.value === 'all') return [fullStart, fullEnd];
      const end = new Date(start);
      end.setMonth(end.getMonth() + Number(periodInput.value));
      return [start, end];
    }}

    function render() {{
      const [start, end] = selectedRange();
      const shown = jobs.filter(job => {{
        const earliest = new Date(job.earliest);
        const latest = job.latest ? new Date(job.latest) : end;
        return earliest <= end && latest >= start;
      }});
      const trains = new Map();
      shown.forEach(job => {{
        if (!trains.has(job.train_id)) trains.set(job.train_id, []);
        trains.get(job.train_id).push(job);
      }});
      const deadlines = shown.filter(job => job.status === 'pending' && job.latest).map(job => new Date(job.latest));
      document.getElementById('job-count').textContent = shown.length;
      document.getElementById('train-count').textContent = trains.size;
      document.getElementById('nearest').textContent = text(deadlines.length ? new Date(Math.min(...deadlines)) : null);
      document.getElementById('last').textContent = text(deadlines.length ? new Date(Math.max(...deadlines)) : null);
      document.getElementById('range-start').textContent = text(start);
      document.getElementById('range-end').textContent = text(end);

      const counts = {{}};
      shown.forEach(job => counts[job.cycle_code] = (counts[job.cycle_code] || 0) + 1);
      document.getElementById('legend').innerHTML = Object.keys(counts).sort().map(cycle =>
        '<span class="legend"><i style="background:' + (colors[cycle] || '#334155') + '"></i>' +
        escapeText(cycle) + ': ' + counts[cycle] + '</span>'
      ).join('');

      const seconds = Math.max((end - start) / 1000, 1);
      document.getElementById('timeline').innerHTML = [...trains.entries()].sort().map(([train, trainJobs]) => {{
        const bars = trainJobs.map(job => {{
          const earliest = new Date(job.earliest);
          const latest = job.latest ? new Date(job.latest) : end;
          const visibleStart = earliest < start ? start : earliest;
          const visibleEnd = latest > end ? end : latest;
          const left = (visibleStart - start) / 1000 / seconds * 100;
          const width = Math.max((visibleEnd - visibleStart) / 1000 / seconds * 100, 0.8);
          const color = job.status === 'pending' ? (colors[job.cycle_code] || '#334155') : '#dc2626';
          const title = escapeText(job.cycle_code + ': ' + text(earliest) + ' — ' + text(latest));
          return '<span class="bar" title="' + title + '" style="left:' + left.toFixed(2) + '%;width:' +
            width.toFixed(2) + '%;background:' + color + '">' + escapeText(job.cycle_code) + '</span>';
        }}).join('');
        return '<div class="timeline-row"><div class="train">' + escapeText(train) +
          '</div><div class="track">' + bars + '</div></div>';
      }}).join('') || '<p>no jobs in this period</p>';

      document.getElementById('jobs-table').innerHTML = shown.sort((a, b) =>
        (a.latest || '9999').localeCompare(b.latest || '9999') || a.train_id.localeCompare(b.train_id)
      ).map(job => '<tr><td>' + escapeText(job.job_id) + '</td><td>' + escapeText(job.train_id) +
        '</td><td><span class="badge">' + escapeText(job.cycle_code) + '</span></td><td>' +
        text(new Date(job.earliest)) + '</td><td>' + text(job.latest ? new Date(job.latest) : null) +
        '</td><td>' + escapeText(job.downtime_hours) + ' h</td><td>' + escapeText(job.status) +
        '</td></tr>').join('') || '<tr><td colspan="7">no jobs in this period</td></tr>';
    }}

    monthInput.addEventListener('change', render);
    periodInput.addEventListener('change', render);
    render();
  </script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=Path, default=Path("data/maintenance_jobs.csv"))
    parser.add_argument("--output", type=Path, default=Path("data/maintenance_timeline.html"))
    args = parser.parse_args()

    jobs = read_jobs(args.jobs)
    # make a page that works without a server
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(build_html(jobs), encoding="utf-8")
    print(f"created {args.output}: {len(jobs)} maintenance jobs")


if __name__ == "__main__":
    try:
        main()
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(2)
