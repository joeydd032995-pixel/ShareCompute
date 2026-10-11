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
        now = datetime.datetime.now(datetime.timezone.utc)
        return now.strftime('%Y-%m-%dT%H:%M:%S.') + f'{now.microsecond // 1000:03d}Z'

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
    """
    def __init__(self, log=None, clock=time.monotonic):
        self.clock = clock; self.start = clock(); self.phases = []; self.log = log

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

    def span(self, name, **fields):
        at = round((self.clock() - self.start) * 1000, 1)
        record = {'name': name, 'at_ms': at, 'duration_ms': None, 'ok': None, **fields}
        self.phases.append(record)
        if self.log: self.log.event('phase_start', phase=name, **fields)
        return Timeline._Span(self, record, name, dict(fields))

    def summary(self):
        """The phases, plus the accounted and unaccounted share of the elapsed time.

        `unaccounted_ms` is the gap F43 could not explain. Spans may not overlap for it to mean
        anything, so only top-level spans are timed.
        """
        total = round((self.clock() - self.start) * 1000, 1)
        measured = sum(p['duration_ms'] or 0 for p in self.phases)
        return {'total_ms': total, 'measured_ms': round(measured, 1),
                'unaccounted_ms': round(total - measured, 1), 'phases': self.phases}
