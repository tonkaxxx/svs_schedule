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
    "IS100": "#2563eb",
    "IS200": "#16a34a",
    "IS510": "#9333ea",
    "IS520": "#ea580c",
    "IS530": "#db2777",
    "IS540": "#dc2626",
    "IS600": "#0891b2",
    "IS700": "#4b5563",
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
  <title>План технического обслуживания</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; background: #fff; color: #171717; font: 14px/1.4 Tahoma, Verdana, Arial, sans-serif; }}
    main {{ max-width: 1280px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0; font-size: 21px; font-weight: 600; }}
    h2 {{ margin: 28px 0 8px; font-size: 14px; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; }}
    .sub {{ margin: 3px 0 0; color: #666; font-size: 13px; }}
    .cards {{ display: flex; flex-wrap: wrap; gap: 0; margin-top: 16px; border-top: 1px solid #d4d4d4; border-bottom: 1px solid #d4d4d4; }}
    .card {{ min-width: 170px; padding: 10px 22px 10px 0; margin-right: 22px; border-right: 1px solid #d4d4d4; }}
    .card:last-child {{ border-right: 0; }}
    .card small {{ display: block; color: #666; font-size: 12px; }}
    .card strong {{ display: block; margin-top: 2px; font-size: 16px; font-weight: 600; }}
    .controls {{ display: flex; flex-wrap: wrap; gap: 14px; align-items: end; margin-top: 18px; }}
    label {{ display: grid; gap: 4px; color: #555; font-size: 12px; }}
    input, select {{ min-height: 30px; padding: 3px 6px; border: 1px solid #999; border-radius: 0; background: #fff; color: #171717; font: inherit; }}
    .panel {{ overflow-x: auto; border: 1px solid #d4d4d4; }}
    .timeline-row {{ display: grid; grid-template-columns: 125px minmax(700px, 1fr); min-height: 28px; border-bottom: 1px solid #e5e5e5; }}
    .train {{ padding: 6px 8px; border-right: 1px solid #d4d4d4; font-family: monospace; font-size: 12px; }}
    .track {{ position: relative; min-height: 28px; background: repeating-linear-gradient(90deg, #fff 0, #fff calc(10% - 1px), #e5e5e5 calc(10% - 1px), #e5e5e5 10%); }}
    .bar {{ position: absolute; top: 5px; height: 18px; min-width: 5px; overflow: visible; border-radius: 0; font-size: 11px; line-height: 16px; white-space: nowrap; }}
    .bar-code {{ display: inline-block; height: 18px; padding: 0 4px; background: #fff; border: 1px solid currentColor; font-weight: 700; }}
    .range {{ display: flex; justify-content: space-between; margin: 5px 8px 5px 125px; color: #666; font-size: 11px; }}
    .legend {{ display: inline-flex; align-items: center; gap: 4px; margin: 0 12px 7px 0; color: #555; font-size: 12px; }}
    .legend i {{ width: 9px; height: 9px; border-radius: 0; }}
    table {{ width: 100%; border-collapse: collapse; white-space: nowrap; }}
    th, td {{ padding: 7px 9px; text-align: left; border-bottom: 1px solid #e5e5e5; }}
    th {{ color: #555; background: #f7f7f7; font-size: 12px; font-weight: 600; }}
    .badge {{ font-family: monospace; }}
    @media (max-width: 700px) {{ main {{ padding: 16px; }} .card {{ min-width: 50%; margin-right: 0; padding-right: 10px; }} }}
  </style>
</head>
<body>
  <main>
    <h1>План технического обслуживания</h1>
    <p class="sub">окна работ по текущему прогнозу</p>
    <section class="controls">
      <label>Месяц начала<input id="start-month" type="month"></label>
      <label>Период<select id="period">
        <option value="1">1 месяц</option>
        <option value="3">3 месяца</option>
        <option value="6">6 месяцев</option>
        <option value="12">1 год</option>
        <option value="24">2 года</option>
        <option value="all">все данные</option>
      </select></label>
    </section>
    <section class="cards">
      <div class="card"><small>Задач</small><strong id="job-count">—</strong></div>
      <div class="card"><small>Поездов</small><strong id="train-count">—</strong></div>
      <div class="card"><small>Ближайший срок</small><strong id="nearest">—</strong></div>
      <div class="card"><small>Последний срок</small><strong id="last">—</strong></div>
    </section>
    <h2>Временная шкала</h2>
    <div id="legend"></div>
    <section class="panel">
      <div class="range"><span id="range-start"></span><span id="range-end"></span></div>
      <div id="timeline"></div>
    </section>
    <h2>Задачи</h2>
    <section class="panel">
      <table>
        <thead><tr><th>Задача</th><th>Поезд</th><th>Цикл</th><th>Начало окна</th><th>Крайний срок</th><th>Работа</th><th>Статус</th></tr></thead>
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
            width.toFixed(2) + '%;background:' + color + '"><span class="bar-code" style="color:' +
            color + '">' + escapeText(job.cycle_code) + '</span></span>';
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
