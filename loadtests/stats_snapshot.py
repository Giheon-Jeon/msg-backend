"""Persist the final Locust counters after users stop, independently of CSV polling."""

import json
from pathlib import Path

from locust import events


@events.quitting.add_listener
def save_final_stats(environment, **kwargs):
    prefix = getattr(environment.parsed_options, "csv_prefix", None)
    if not prefix:
        return

    def snapshot(entry):
        row = dict(entry.serialize())
        row.update(
            avg_ms=entry.avg_response_time,
            p95_ms=entry.get_response_time_percentile(0.95),
            p99_ms=entry.get_response_time_percentile(0.99),
            rps=entry.total_rps,
        )
        return row

    data = {
        "total": snapshot(environment.stats.total),
        "entries": [snapshot(entry) for entry in environment.stats.entries.values()],
    }
    Path(prefix + "-final.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
