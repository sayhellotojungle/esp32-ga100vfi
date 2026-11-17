#!/usr/bin/env python3
"""Synchronize shared firmware/partitions into the Arduino sketch, or verify it."""
from pathlib import Path
import argparse
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', help='fail if the sketch differs')
    args = ap.parse_args()
    root = Path(__file__).resolve().parents[1]
    sketch = root / 'arduino' / 'ga100vfi'
    sources = sorted((root / 'main').glob('*.c')) + sorted((root / 'main').glob('*.h'))
    sources += [root / 'partitions.csv']
    expected = {p.name for p in sources}
    different = []
    for source in sources:
        dest = sketch / source.name
        if not dest.is_file() or dest.read_bytes() != source.read_bytes():
            different.append(source.name)
            if not args.check:
                sketch.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(source.read_bytes())
    stale = [p for p in sketch.iterdir() if p.suffix in {'.c', '.h'} and p.name not in expected]
    if stale:
        print('Unexpected sketch source files (review/remove manually): ' + ', '.join(p.name for p in stale), file=sys.stderr)
        return 1
    if args.check and different:
        print('Arduino copies differ: ' + ', '.join(different), file=sys.stderr)
        return 1
    print(f'Arduino source copies {"verified" if args.check else "synchronized"}: {len(sources)} files')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
