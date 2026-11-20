#!/usr/bin/env python3
"""Package the Arduino build into firmware/, named with the firmware version, and bind
each published image to the firmware sources it was compiled from.

usage:
    python tools/sync_arduino.py && arduino-cli compile --fqbn ... --build-path build-arduino arduino/ga100vfi
    python tools/package_firmware.py --prune        # after a build
    python tools/package_firmware.py --check        # publish gate: do the zips match the sources?

The app image is what OTA accepts; the merged image is the one-shot serial flash at 0x0.
Both are published zipped, each holding a single file named
`esp32-ga100vfi-<version>-{app,merged}.bin`, which is the layout the zips have always had.

Why the source digest exists: "which sources produced this image" was answerable only by
trusting whoever ran the build last, and the bench lost days to that question. The stamp
compiled into the binary (__DATE__/__TIME__) names the build moment; the digest names the
inputs. --check recomputes the digest and fails when they disagree, so a source change
cannot ship as "the published firmware".
"""
import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = {"app": "ga100vfi.ino.bin", "merged": "ga100vfi.ino.merged.bin"}
RELEASE = "firmware/RELEASE.json"
# Everything the compiler sees for the app image. Arduino copies are generated from main/,
# and included because they are what arduino-cli actually reads.
SOURCE_GLOBS = ["main/*.c", "main/*.h", "main/webui.html", "partitions.csv",
                "arduino/ga100vfi/*.c", "arduino/ga100vfi/*.h", "arduino/ga100vfi/*.ino",
                "arduino/ga100vfi/partitions.csv", "CMakeLists.txt"]
STAMP_RE = rb"(\d+\.\d+\.\d+) \(([A-Z][a-z]{2} [ \d]\d \d{4} \d{2}:\d{2}:\d{2})\)"


def source_digest():
    """Digest the firmware sources the way git stores them: line endings are not content.

    Without the CRLF->LF step, a checkout on a different platform (or `core.autocrlf`)
    changes the digest while changing nothing the compiler sees -- which would make the
    publish gate fire on its own environment.
    """
    h = hashlib.sha256()
    files = sorted({p for g in SOURCE_GLOBS for p in ROOT.glob(g)})
    if not files:
        raise SystemExit("no firmware sources found; run tools/sync_arduino.py first")
    for p in files:
        h.update(str(p.relative_to(ROOT).as_posix()).encode())
        h.update(b"\0")
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest(), [p.relative_to(ROOT).as_posix() for p in files]


def version_from_board_h():
    text = (ROOT / "main" / "board.h").read_text(encoding="utf-8")
    m = re.search(r'#define\s+GA_FW_VERSION\s+"([^"]+)"', text)
    if not m:
        raise SystemExit("main/board.h has no GA_FW_VERSION to package against")
    return m.group(1)


def sha256(path_or_bytes):
    data = path_or_bytes if isinstance(path_or_bytes, bytes) else Path(path_or_bytes).read_bytes()
    return hashlib.sha256(data).hexdigest()


def embedded_stamp(app_bytes):
    m = re.search(STAMP_RE, app_bytes)
    if not m:
        raise SystemExit("app image carries no `X.Y.Z (Mon DD YYYY HH:MM:SS)` build stamp: "
                         "main/board.h's GA_BUILD is not reaching /api/status")
    return m.group(1).decode(), m.group(2).decode()


def git_state():
    def run(*a):
        return subprocess.run(["git"] + list(a), cwd=ROOT, capture_output=True,
                               text=True).stdout.strip()
    return {"commit": run("rev-parse", "--short", "HEAD") or None,
            "dirty": bool(run("status", "--porcelain"))}


def package(version, build, out, prune):
    out.mkdir(parents=True, exist_ok=True)
    if not build.is_dir():
        raise SystemExit(f"{build} is not a build directory; compile first")
    record = {"version": version, "images": {}, "source": {}, "git": git_state()}
    app_bytes = None
    for kind, src_name in IMAGES.items():
        src = build / src_name
        if not src.is_file():
            raise SystemExit(f"missing {src}: compile with --build-path {build.name} first")
        raw = src.read_bytes()
        if kind == "app":
            app_bytes = raw
        bin_name = f"esp32-ga100vfi-{version}-{kind}.bin"
        dst = out / (bin_name + ".zip")
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(src, bin_name)
        record["images"][kind] = {"file": dst.relative_to(ROOT).as_posix(), "bin": bin_name,
                                  "bin_size": len(raw), "bin_sha256": sha256(raw),
                                  "zip_size": dst.stat().st_size, "zip_sha256": sha256(dst)}
        print(f"{dst.relative_to(ROOT)}  {len(raw):>9} B bin  {record['images'][kind]['zip_size']:>8} B zip")

    img_version, stamp = embedded_stamp(app_bytes)
    if img_version != version:
        raise SystemExit(f"image says {img_version}, main/board.h says {version}: "
                         "recompile (and tools/sync_arduino.py) before packaging")
    digest, files = source_digest()
    record["stamp"] = stamp
    record["source"] = {"sha256": digest, "files": len(files)}
    (out / Path(RELEASE).name).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n",
                                          encoding="utf-8", newline="\n")
    print(f"{RELEASE}  stamp={stamp}  sources={len(files)}  sha256={digest[:16]}…  git={record['git']}")

    if prune:
        keep = {f"esp32-ga100vfi-{version}-{k}.bin.zip" for k in IMAGES}
        for old in out.glob("esp32-ga100vfi-*.bin.zip"):
            if old.name not in keep:
                old.unlink()
                print("pruned", old.relative_to(ROOT))
    return 0


def check(out):
    """Gate: the published zips must be the ones built from the sources now in the tree."""
    rel = out / Path(RELEASE).name
    if not rel.is_file():
        raise SystemExit(f"{rel} missing: package the images first")
    record = json.loads(rel.read_text(encoding="utf-8"))
    version = version_from_board_h()
    if record.get("version") != version:
        print(f"FAIL: {RELEASE} says {record.get('version')}, main/board.h says {version}")
        return 1
    digest, files = source_digest()
    if record["source"]["sha256"] != digest:
        print("FAIL: firmware sources changed since these images were built")
        print(f"      published: {record['source']['sha256'][:16]}…  now: {digest[:16]}…")
        print("      re-run tools/sync_arduino.py, arduino-cli compile, then package_firmware.py")
        return 1
    for kind, info in record["images"].items():
        p = ROOT / info["file"]
        if not p.is_file():
            print(f"FAIL: {info['file']} is missing")
            return 1
        if not zipfile.ZipFile(p).namelist() == [info["bin"]]:
            print(f"FAIL: {p.name} does not contain a single {info['bin']}")
            return 1
        if sha256(p) != info["zip_sha256"]:
            print(f"FAIL: {p.name} sha256 drifted from {RELEASE}")
            return 1
        if sha256(zipfile.ZipFile(p).read(info["bin"])) != info["bin_sha256"]:
            print(f"FAIL: {info['bin']} sha256 drifted from {RELEASE}")
            return 1
    print(f"OK: {record['version']} images (stamp {record['stamp']}) match {len(files)} firmware "
          f"sources (sha256 {digest[:16]}…), built at git {record['git'].get('commit')}")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--build-dir", default="build-arduino")
    ap.add_argument("--out", default="firmware")
    ap.add_argument("--version", default=None)
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--check", action="store_true", help="verify firmware/ against the sources")
    args = ap.parse_args()
    out = ROOT / args.out
    if args.check:
        return check(out)
    return package(args.version or version_from_board_h(), ROOT / args.build_dir, out, args.prune)


if __name__ == "__main__":
    sys.exit(main())
