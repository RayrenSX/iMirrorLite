"""Read actual SurfaceFlinger presentation timestamps for the mirroring layer."""
import argparse
import json
from pathlib import Path
import re
import statistics
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--adb', type=Path, default=Path(__file__).resolve().parents[1]
                        / '.tools/android-sdk/platform-tools/adb.exe')
    args = parser.parse_args()
    if args.seconds <= 0:
        parser.error('--seconds must be positive')
    base = [str(args.adb), '-s', args.serial]

    def shell(command):
        return subprocess.check_output(base + ['shell', command], timeout=8,
                                       encoding='utf-8', errors='replace')

    layers = shell('dumpsys SurfaceFlinger --list').splitlines()
    # Some Android 15 vendors wrap layer names in RequestedLayerState{...}.
    pattern=r'SurfaceView\[com\.rayren\.imirrorlite/com\.rayren\.imirrorlite\.MainActivity\]\(BLAST\)#[0-9]+'
    matches = sorted({match.group(0) for row in layers for match in re.finditer(pattern,row)})
    if len(matches) != 1:
        raise RuntimeError(f'Expected one mirroring video layer, found {matches}')
    layer = matches[0]
    command = f"dumpsys SurfaceFlinger --latency '{layer}'"

    def sample():
        raw = shell(command)
        lines = raw.splitlines()
        if not lines or not lines[0].isdigit():
            raise RuntimeError('SurfaceFlinger did not return presentation timestamps')
        stamps = set()
        for row in lines[1:]:
            fields = row.split()
            if len(fields) == 3:
                stamp = int(fields[1])
                if 0 < stamp < 2**63 - 1:
                    stamps.add(stamp)
        return int(lines[0]), stamps, raw

    _, initial, _ = sample()
    cutoff = max(initial, default=0)
    stamps, captures, refresh_periods = set(), [], set()
    started = time.monotonic()
    while time.monotonic() - started < args.seconds:
        period, latest, raw = sample()
        refresh_periods.add(period)
        stamps.update(v for v in latest if v > cutoff)
        captures.append({'elapsed_seconds': time.monotonic()-started, 'raw': raw})
        time.sleep(min(.75, max(0, args.seconds-(time.monotonic()-started))))
    period, latest, raw = sample()
    refresh_periods.add(period)
    stamps.update(v for v in latest if v > cutoff)
    captures.append({'elapsed_seconds': time.monotonic()-started, 'raw': raw})
    ordered = sorted(stamps)
    if len(ordered) < 2:
        raise RuntimeError('No continuing video presentation during measurement')
    intervals = [(b-a)/1e6 for a, b in zip(ordered, ordered[1:])]
    result = {
        'serial': args.serial, 'layer': layer,
        'elapsed_seconds': time.monotonic()-started,
        'refresh_period_ns': sorted(refresh_periods),
        'presented_frames': len(ordered),
        'fps': (len(ordered)-1)*1e9/(ordered[-1]-ordered[0]),
        'median_interval_ms': statistics.median(intervals),
        'max_interval_ms': max(intervals),
        'intervals_over_25_ms': sum(v > 25 for v in intervals),
        'presentation_timestamps_ns': ordered, 'captures': captures,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ('presentation_timestamps_ns', 'captures')}, indent=2))
    print(f'Evidence: {args.output.resolve()}')


if __name__ == '__main__':
    main()
