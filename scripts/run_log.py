#!/usr/bin/env python3
"""Measurement for one split run: a phase timeline, a machine-readable event log, and host memory.

Three gaps in the record motivate each piece, and each is named where it is used:

* F43 could not say where 115 of the warm run's 141 seconds went, because nothing timed the phases
  between joining and generating. `Timeline` times them.
* F42's cause is unsettleable because no log of the failure was ever captured. `EventLog` writes one
  line per event to a file that survives the run, so the next failure has a record.
* F43 recorded no memory headroom on a 4 GB laptop. `host_memory` samples it.

Nothing here may fail a run: an unreadable counter is recorded as absent, never raised. Reporting
`null` is honest; guessing is not, and crashing an overnight physical run over a metric is worse than
both.
"""
import datetime
import json
import os
from pathlib import Path
import sys
import time

def host_memory():
    """Total and available physical memory, or None per field where the platform will not say.

    Deliberately dependency-free: the laptop kit ships a frozen Python and adding psutil to it for
    two numbers is not worth the packaging weight.

    **May block.** The macOS path spawns `sysctl` and `vm_stat`. Never call this directly from an
    asyncio loop — F41 is this project's record of what blocking that loop costs: every connected
    device is dropped. The coordinator calls it through `asyncio.to_thread`.
    """
    try:
        if sys.platform.startswith('linux'):
            values = {}
            for line in Path('/proc/meminfo').read_text().splitlines():
                key, _, rest = line.partition(':')
                if key in ('MemTotal', 'MemAvailable'): values[key] = int(rest.split()[0]) * 1024
            return {'total_bytes': values.get('MemTotal'), 'available_bytes': values.get('MemAvailable')}
        if os.name == 'nt':
            import ctypes
            class Status(ctypes.Structure):
                _fields_ = [('dwLength', ctypes.c_ulong), ('dwMemoryLoad', ctypes.c_ulong),
                            ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                            ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                            ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                            ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
            status = Status(); status.dwLength = ctypes.sizeof(Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return {'total_bytes': status.ullTotalPhys, 'available_bytes': status.ullAvailPhys}
        if sys.platform == 'darwin':
            import subprocess
            total = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True, timeout=5))
            # vm_stat reports pages; free plus inactive is the closest analogue of MemAvailable.
            out = subprocess.check_output(['vm_stat'], text=True, timeout=5)
            size = int(out.split('page size of')[1].split('bytes')[0])
            pages = {}
            for line in out.splitlines()[1:]:
                key, _, rest = line.partition(':')
                pages[key.strip()] = int(rest.strip().rstrip('.') or 0)
            free = (pages.get('Pages free', 0) + pages.get('Pages inactive', 0)) * size
            return {'total_bytes': total, 'available_bytes': free}
    except Exception:
        pass
    return {'total_bytes': None, 'available_bytes': None}

def utc_text(moment):
    """A UTC datetime in the same millisecond format every `ts` in every log uses."""
    return moment.strftime('%Y-%m-%dT%H:%M:%S.') + f'{moment.microsecond // 1000:03d}Z'

class EventLog:
    """One JSON object per line, flushed as it goes, so a killed run still leaves its history.

    Append-only and bounded only by disk: the whole point is to survive the failure that produced it,
    so nothing is buffered for later and nothing is overwritten.

    Every record carries both clocks. `at_ms` is elapsed milliseconds since this log began, which is
    what you read within one log; `ts` is UTC, which is the only way to line this log up against the
    phones' own exports. The phone processes start long before a run, so their elapsed values have a
    different origin entirely — without `ts` the three logs cannot be ordered, and "which device saw
    the failure first" is the question F42 could not answer. iOS writes the same `ts`; Android writes
    the identical field as `wall`.

    **A logging failure never reaches the caller.** A full, read-only or detached disk makes `write`
    raise, and these calls sit inside the coordinator's own coroutines and its cleanup path, so an
    escaping error would take out the run and the report with it — the diagnostic facility failing
    the thing it exists to diagnose. The file sink is dropped on the first error and the in-memory
    records continue, so `report.json` still carries the timeline.
    """
    def __init__(self, path=None, echo=None, clock=time.monotonic):
        self.path = Path(path) if path else None
        self.clock = clock; self.start = clock(); self.echo = echo; self.records = []
        self.handle = None; self.write_error = None
        if self.path:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self.handle = self.path.open('w', encoding='utf-8')
            except OSError as error:
                self.write_error = f'{type(error).__name__}: {error}'

    @staticmethod
    def _utc():
        # One reading, formatted to milliseconds: two calls to now() could straddle a millisecond
        # boundary and stamp a record with a time it was never at.
        return utc_text(datetime.datetime.now(datetime.timezone.utc))

    def event(self, name, **fields):
        record = {'at_ms': round((self.clock() - self.start) * 1000, 1), 'ts': self._utc(), 'ev': name, **fields}
        self.records.append(record)
        if self.handle:
            try:
                self.handle.write(json.dumps(record, separators=(',', ':'), default=str) + '\n')
                self.handle.flush()
            except (OSError, ValueError) as error:
                # Dropped rather than retried: if the disk is full every later line fails too, and
                # one failed write per event would be a second failure mode on top of the first.
                self.write_error = f'{type(error).__name__}: {error}'
                try: self.handle.close()
                except OSError: pass
                self.handle = None
        if self.echo:
            try: self.echo(record)
            except Exception: pass  # An echo is a convenience for the operator's terminal, never a run's fate.
        return record

    def close(self):
        if self.handle:
            try: self.handle.close()
            except OSError as error: self.write_error = self.write_error or f'{type(error).__name__}: {error}'
            self.handle = None

class Timeline:
    """Durations of the phases of a run, in the order they happened.

    `span` is for work with a clear start and end; `mark` is for a moment, such as a worker joining.
    A phase that raises is still recorded, with `ok: False` — a failed run's timings are the
    interesting ones.

    A span may name a `parent`. Children are detail *inside* their parent, so `summary` counts only
    parentless spans toward `measured_ms`; counting both would report more time than the run had.
    """
    def __init__(self, log=None, clock=time.monotonic):
        self.clock = clock; self.start = clock(); self.phases = []; self.log = log
        # One wall reading so a child observed earlier can be stamped with when it happened, not
        # with when it was written down. Reading the injected clock again would shift its ticks.
        self.wall_start = datetime.datetime.now(datetime.timezone.utc)

    def mark(self, name, **fields):
        at = round((self.clock() - self.start) * 1000, 1)
        self.phases.append({'name': name, 'at_ms': at, 'duration_ms': None, 'ok': True, **fields})
        if self.log: self.log.event('mark', phase=name, **fields)
        return at

    class _Span:
        def __init__(self, timeline, record, name, fields):
            self.timeline = timeline; self.record = record; self.name = name; self.fields = fields
            self.began = timeline.clock()
        def __enter__(self): return self
        def note(self, **fields):
            """Attach measurements discovered while the phase ran, such as bytes or throughput."""
            self.record.update(fields); self.fields.update(fields)
        def __exit__(self, kind, value, tb):
            self.record['duration_ms'] = round((self.timeline.clock() - self.began) * 1000, 1)
            self.record['ok'] = kind is None
            if self.timeline.log:
                self.timeline.log.event('phase', phase=self.name, duration_ms=self.record['duration_ms'],
                                        ok=self.record['ok'], **self.fields)
            return False

    def span(self, name, parent=None, **fields):
        at = round((self.clock() - self.start) * 1000, 1)
        if parent is not None: fields = {'parent': parent, **fields}
        record = {'name': name, 'at_ms': at, 'duration_ms': None, 'ok': None, **fields}
        self.phases.append(record)
        if self.log: self.log.event('phase_start', phase=name, **fields)
        return Timeline._Span(self, record, name, dict(fields))

    def _wall(self, reading):
        return utc_text(self.wall_start + datetime.timedelta(seconds=reading - self.start))

    def add_span(self, name, parent, began, ended, ok=True, **fields):
        """Record a child span whose start and end were observed elsewhere.

        `began` and `ended` are readings of this timeline's own clock. Nothing here reads the clock,
        so adding a child never shifts the timing of the spans around it. `ok=False` marks a child
        that was cut short, such as one observed in a run that then failed. The event carries the
        observed times (`began_ts`, `ended_ts`, UTC) because the log line itself is written later.
        """
        record = {'name': name, 'at_ms': round((began - self.start) * 1000, 1),
                  'duration_ms': round((ended - began) * 1000, 1), 'ok': ok, 'parent': parent, **fields}
        self.phases.append(record)
        if self.log:
            self.log.event('phase', phase=name, parent=parent, duration_ms=record['duration_ms'], ok=ok,
                           began_ms=record['at_ms'], ended_ms=round((ended - self.start) * 1000, 1),
                           began_ts=self._wall(began), ended_ts=self._wall(ended), **fields)
        return record

    def summary(self):
        """The phases, plus the accounted and unaccounted share of the elapsed time.

        `unaccounted_ms` is the gap F43 could not explain. Spans may not overlap for it to mean
        anything, so only top-level spans are timed: a child (one with a `parent`) is detail inside
        its parent and is left out of the sum.
        """
        total = round((self.clock() - self.start) * 1000, 1)
        measured = sum(p['duration_ms'] or 0 for p in self.phases if 'parent' not in p)
        return {'total_ms': total, 'measured_ms': round(measured, 1),
                'unaccounted_ms': round(total - measured, 1), 'phases': self.phases}

class TransferWatch:
    """When cumulative counters did their bulk of moving, from periodic samples.

    A run's biggest phase is one subprocess call, so its inside is invisible from the coordinator
    except through counters that already exist: bytes the relay has forwarded to each worker, and
    the workers' cache counters. Feed `observe` a sample of each on a timer and `spans` says when
    each node's counter was doing its work. That is F45's missing "when did each phone start
    receiving, and when did it stop" -- recovered at the sampler's resolution instead of from the
    phones' own log lines, which they coalesce to 5-10 s.

    The window is from the moment the counter had covered `low` of its eventual total to the moment
    it had covered `high` of it, not from its first change to its last. A relay counter also counts
    the handshake at connect and the small per-token traffic until the run ends, so first and last
    change would span the connection's whole life and say nothing about the weights (measured on a
    loopback run before this was changed: all three nodes "began" in the same sample and "ended"
    at the end of the run).

    The first sample of a counter is its baseline, so only movement during the watch is reported.
    A counter that goes backwards (a worker process restarted) is re-baselined, and what it had
    already counted is forgotten. `resolution_ms` is the longest gap between two samples of that
    counter: either edge of the window is that uncertain. Feed it the time a value was *produced*,
    not the time it was polled: the workers' cache counters arrive as heartbeat snapshots every
    250-500 ms, and polling one snapshot ten times would claim 100 ms.

    A counter that only moves when a whole unit of work finishes (the cache's `hit` and `stored`,
    which advance after a tensor has been hashed, read and installed) reports its first unit's
    completion, not its start. `first_step` is the size of the step that crossed `low` and
    `first_step_share` its fraction of the total: when that is large, `began` is when the first
    unit finished and the work on it happened before, for an unknown time.
    """
    def __init__(self, low=0.01, high=0.99):
        self.low, self.high, self.series, self.stamps = low, high, {}, {}

    def observe(self, now, node, group, **values):
        key = (group, node)
        item = self.series.get(key)
        if item is None:
            self.series[key] = {'base': dict(values), 'last': dict(values), 'seen': now, 'gap': 0.0, 'points': []}
            return
        item['gap'] = max(item['gap'], now - item['seen']); item['seen'] = now
        if any(values[name] < item['last'][name] for name in values):
            item.update(base=dict(values), last=dict(values), points=[]); return
        if any(values[name] != item['last'][name] for name in values):
            item['points'].append((now, sum(values[name] - item['base'][name] for name in values)))
        item['last'] = dict(values)

    def spans(self, until=None):
        """One entry per counter that moved, in the order the bulk of its movement began.

        `until` clamps both edges. A snapshot that reports work after the run's measured phase has
        ended (a heartbeat lagging the probe's exit) describes work that happened before it.
        """
        found = []
        for (group, node), item in self.series.items():
            points = item['points']
            if not points: continue
            total = points[-1][1]
            first = next(n for n, (_, moved) in enumerate(points) if moved >= self.low * total)
            last = next(n for n, (_, moved) in enumerate(points) if moved >= self.high * total)
            began, ended = points[first][0], points[last][0]
            if until is not None: began, ended = min(began, until), min(ended, until)
            step = points[first][1] - (points[first - 1][1] if first else 0)
            moved = {name: item['last'][name] - item['base'][name] for name in item['last']}
            found.append({'name': f'{group}:{node}', 'began': began, 'ended': ended,
                          'resolution_ms': round(item['gap'] * 1000, 1), 'first_step': step,
                          'first_step_share': round(step / total, 3), **moved})
        return sorted(found, key=lambda entry: entry['began'])
