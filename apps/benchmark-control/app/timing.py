"""Audit retained structural timing evidence without rewriting native metrics."""
from __future__ import annotations

import math


# Timestamps cross scheduler processes; allow small reporting/serialization skew.
TIME_TOLERANCE_SECONDS = 0.01


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def count(value):
    return finite(value) and value >= 0 and int(value) == value


def audit_timing(benchmark):
    """Full-run throughput includes all outcome lifetimes and successful tokens only.

    No reconstruction from sampled/truncated records, requested token budgets, or
    summary duration. Native window consistency is independent of reconstruction.
    """
    scheduler = benchmark.get('scheduler_metrics') or {}
    start, end = scheduler.get('measure_start_time'), scheduler.get('measure_end_time')
    execution_start, execution_end = scheduler.get('start_time'), scheduler.get('end_time')
    native_findings = []
    native_status = 'not_checked'
    if finite(start) and finite(end) and start >= 0 and end >= 0:
        native_status = 'consistent'
        if end <= start:
            native_findings.append('Native measurement window has non-positive duration')
        if finite(execution_start) and execution_start >= 0 and start < execution_start - TIME_TOLERANCE_SECONDS:
            native_findings.append('Native measurement starts before scheduler execution')
        if finite(execution_end) and execution_end >= 0 and end > execution_end + TIME_TOLERANCE_SECONDS:
            native_findings.append('Native measurement ends after scheduler execution')
        if not (finite(execution_start) and finite(execution_end) and execution_start >= 0 and execution_end >= execution_start):
            native_status = 'not_checked'
        duration = benchmark.get('duration')
        if finite(duration) and not math.isclose(duration, end-start, rel_tol=1e-6, abs_tol=TIME_TOLERANCE_SECONDS):
            native_findings.append('Native duration disagrees with its measurement boundaries')
    cooldown = benchmark.get('cooldown_duration')
    if finite(cooldown) and cooldown < -TIME_TOLERANCE_SECONDS:
        native_findings.append('Native cooldown duration is negative')
    if native_findings:
        native_status = 'inconsistent'

    findings = []
    requests = benchmark.get('requests') or {}
    metrics = benchmark.get('metrics') or {}
    totals = metrics.get('request_totals') or {}
    records = []
    successful = []
    for status in ('successful', 'errored', 'incomplete'):
        retained = requests.get(status)
        if not isinstance(retained, list) or not count(totals.get(status)) or len(retained) != totals[status]:
            findings.append(f'Retained {status} records missing, sampled or inconsistent with counts')
        elif not all(isinstance(record, dict) for record in retained):
            findings.append(f'Invalid retained {status} records')
        else:
            records.extend(retained)
            if status == 'successful':
                successful = retained
    if not count(totals.get('total')) or len(records) != totals['total'] or not records:
        findings.append('Retained request total is missing, empty or inconsistent')
    ids = [record.get('request_id') for record in records]
    if any(not isinstance(identifier, str) or not identifier for identifier in ids) or len(set(i for i in ids if isinstance(i,str))) != len(ids):
        findings.append('Request identities are missing or duplicated')
    valid_intervals = all(finite(record.get('request_start_time')) and finite(record.get('request_end_time'))
                          and 0 <= record['request_start_time'] <= record['request_end_time'] for record in records)
    if not valid_intervals:
        findings.append('Request timestamps are missing, non-finite or reversed')
    valid_tokens = all(count(record.get('output_tokens')) for record in successful)
    if not valid_tokens:
        findings.append('Actual successful output token counts are missing or invalid')
    tokens = sum(record['output_tokens'] for record in successful) if valid_tokens else None
    token_summary = (metrics.get('output_token_count') or {}).get('successful') or {}
    if not count(token_summary.get('count')) or token_summary['count'] != len(successful) or not count(token_summary.get('total_sum')) or tokens != token_summary['total_sum']:
        findings.append('Successful output token records do not reconcile with the native summary')
    request_start = min((record['request_start_time'] for record in records), default=None) if valid_intervals else None
    request_end = max((record['request_end_time'] for record in records), default=None) if valid_intervals else None
    duration = request_end-request_start if request_start is not None else None
    if duration is None or duration <= 0:
        findings.append('Full request window has no positive duration')
    if request_start is not None:
        # Check against independent scheduler execution and request boundaries.
        for key, observed in (('request_start_time',request_start), ('request_end_time',request_end)):
            boundary = scheduler.get(key)
            if not finite(boundary) or boundary < 0 or abs(boundary-observed) > TIME_TOLERANCE_SECONDS:
                findings.append(f'Retained requests do not reconcile with scheduler {key}')
        if not (finite(execution_start) and finite(execution_end) and 0 <= execution_start <= execution_end):
            findings.append('Scheduler execution boundaries are unavailable')
        elif request_start < execution_start - TIME_TOLERANCE_SECONDS or request_end > execution_end + TIME_TOLERANCE_SECONDS:
            findings.append('Retained requests fall outside scheduler execution')
    return dict(native_timing_status=native_status, native_timing_findings=native_findings,
                full_run_status='unavailable' if findings else 'reconciled', full_run_findings=findings,
                full_run_start_time=request_start, full_run_end_time=request_end,
                full_run_duration_seconds=duration if not findings else None,
                full_run_successful_output_tokens=tokens if not findings else None,
                full_run_output_tps=tokens/duration if not findings else None)
