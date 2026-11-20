#!/usr/bin/env python3
"""Regenerate MANIFEST.sha256 from the tracked tree.

usage:
    python tools/gen_manifest.py            # write MANIFEST.sha256
    python tools/gen_manifest.py --check    # fail if it is stale

The hashes are sha256 of the git-normalized content (`text=auto` / `eol=lf` applied),
not of the bytes on disk, so a checkout on Windows and one on Linux produce the same
manifest. That is the same convention the file already used before this script existed.
"""
import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = "MANIFEST.sha256"


def tracked_files():
    # -z is not cosmetic: git quotes non-ASCII path components (this repo tracks a
    # Chinese-named markdown file), and the quoted form is not a path git will accept back.
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True,
                         capture_output=True).stdout
    return [f.decode("utf-8") for f in out.split(b"\0") if f and f != OUT.encode()]


def normalized_bytes(path):
    """Apply git's text filters through the object store, then read the blob back."""
    sha1 = subprocess.run(["git", "hash-object", "-w", "--path", path, "--", path],
                          cwd=ROOT, check=True, capture_output=True,
                          text=True).stdout.strip()
    return subprocess.run(["git", "cat-file", "blob", sha1], cwd=ROOT, check=True,
                          capture_output=True).stdout


def current_lines():
    """The manifest as it sits on disk, LF-normalized (a Windows checkout must not look stale)."""
    if not (ROOT / OUT).is_file():
        return []
    return (ROOT / OUT).read_bytes().decode("utf-8").splitlines()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify, do not write")
    args = ap.parse_args()
    lines = ["%s  %s" % (hashlib.sha256(normalized_bytes(f)).hexdigest(), f)
             for f in tracked_files()]
    text = "\n".join(lines) + "\n"
    if args.check:
        # read_bytes, not read_text(newline=...): the newline argument only exists on
        # 3.13+, and comparing decoded text would let a CRLF checkout look stale.
        def as_map(lines):
            out = {}
            for l in lines:
                digest, _, path = l.partition("  ")
                out[path] = digest
            return out

        have, want = as_map(current_lines()), as_map(lines)
        added = sorted(p for p in want if p not in have)
        removed = sorted(p for p in have if p not in want)
        changed = sorted(p for p in want if p in have and have[p] != want[p])
        if added or removed or changed:
            print("MANIFEST.sha256 is stale: %d new, %d changed, %d obsolete"
                  % (len(added), len(changed), len(removed)), file=sys.stderr)
            for tag, group in (("new", added), ("changed", changed), ("removed", removed)):
                for p in group[:20]:
                    print(f"  [{tag}] {p}", file=sys.stderr)
            return 1
        print("MANIFEST.sha256 current (%d files)" % len(lines))
        return 0
    (ROOT / OUT).write_text(text, encoding="utf-8", newline="\n")
    print("wrote %s (%d files)" % (OUT, len(lines)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
