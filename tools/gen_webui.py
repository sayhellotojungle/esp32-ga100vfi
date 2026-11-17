#!/usr/bin/env python3
"""Regenerate the embedded page from main/webui.html on any platform."""
import argparse
import json
from pathlib import Path


def main(argv=None):
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=root / 'main/webui.html')
    parser.add_argument('--output', type=Path, default=root / 'main/webui.h')
    args = parser.parse_args(argv)
    source = args.source.read_text(encoding='utf-8')
    lines = ['// Generated from main/webui.html by tools/gen_webui.py.',
             '// Edit the HTML source, then regenerate this file.',
             '#pragma once', '', 'static const char WEBUI_HTML[] =']
    lines.extend('    ' + json.dumps(line, ensure_ascii=False)
                 for line in source.splitlines(keepends=True))
    if not source:
        lines.append('    ""')
    lines.append(';')
    args.output.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(f'{args.output.name}: {len(source.encode("utf-8"))} HTML bytes')


if __name__ == '__main__':
    main()
