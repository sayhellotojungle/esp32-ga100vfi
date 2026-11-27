#!/usr/bin/env python3
"""给 ffcalc 快照补上身份字段，让 `ffcalc verify` 真的能跑，而不是被跳过。

为什么需要这一步：认卡的正规通道是 nvidia-smi 的 GPU UUID（`ffcalc ingest` 从
`collected/bar0map.tsv` 第三列取）。这台判决机从来没装过 nvidia 驱动（无 nvidia-smi、
无 nvidia.ko），UUID 通道不存在。而 `ffcalc capture` 走 sysfs resource0，它只写
option_words/info/ff_rows，不写 fingerprint —— 于是 `ffcalc verify` 直接
`FAIL: plan has no uuid/fingerprint; re-generate it`。那不是卡不对，是快照没身份。
上一张卡（0.3.0）用的是 ffprobe.ko + `ffcalc ingest` 的快照，ingest 会算 fingerprint，
verify 以 "belongs to card GPU-<fingerprint>" 通过（0.3.0 实机验证过）。
本工具在 capture 路径上做同一件事。

红线：fingerprint 是 `*_DEFECTIVE` 全字 + SBFE 位的烧写不变哈希，只能证明"烧前烧后是
同一块 die"，**不能证明这是哪一张卡**——同批次卡的 defective 相同、出厂 SBFE 可为 1，
指纹可与他人卡相同（README §2.5）。所以补完之后仍然必须：512 行两次独立读一致、
`ga100vfi.burn crosscheck` 现场逐行回读目标行。UUID 通道一旦可用（装了驱动），
必须回到 UUID 认卡。

哈希实现不许自己写：输入名与算法全部取 ffcalc 自己的 `ingest.fingerprint()` 与它的
map。map 里声明的 defective 字少一个、或 SBFE 缺失，就立即退出——输入集不同则哈希与
规范不可比，那比没有身份更危险。

usage（在判决机上、ffcalc 可 import 时）：
    python3 tools/attach_identity.py card2/dump.json --bdf 0000:01:00.0 \
            --ffcalc /root/ffcalc
    python3 tools/attach_identity.py card2/dump.json --ffcalc /root/ffcalc --check
"""
import argparse
import json
import sys
from pathlib import Path


def load_ffcalc(ffc_calc_dir):
    """Import ffcalc from an explicit directory; no silent fallback to a stub."""
    if ffc_calc_dir:
        sys.path.insert(0, str(Path(ffc_calc_dir).resolve()))
    try:
        from ffcalc import ingest  # noqa: F401
    except ImportError as exc:
        raise SystemExit(f"无法 import ffcalc（--ffcalc 给对了吗）：{exc}")
    try:
        import ffcalc
        if not getattr(ffcalc, "__file__", None):
            raise SystemExit("import 到的是命名空间包（空壳），不是 ffcalc 本体；"
                             f"--ffcalc 应指向含 ffcalc/ 包的仓库根目录，得到 {ffcalc.__path__}")
    except ImportError as exc:
        raise SystemExit(f"ffcalc 校验失败：{exc}")
    return ingest


def build_entries(dump, mmap):
    """把 capture 快照还原成 ingest.fingerprint() 期望的 (name, addr, value) 输入集。

    ingest 侧的名字是 `defective.<key>` 与 `info.secured_burn_feature_enable`；
    capture 侧把值存在 option_words[key] / info[name] 里，名字一一对得上。
    """
    words = dump.get("option_words") or {}
    info = dump.get("info") or {}
    missing = [k for k in mmap["defective"] if k not in words]
    entries = []
    for key in mmap["defective"]:
        if key in missing:
            continue
        entries.append((f"defective.{key}", 0, int(str(words[key]), 0)))
    sbfe_name = "secured_burn_feature_enable"
    if sbfe_name not in info:
        missing.append(f"info.{sbfe_name}")
    else:
        entries.append((f"info.{sbfe_name}", 0, int(str(info[sbfe_name]), 0)))
    if missing:
        raise SystemExit("快照缺少指纹输入，哈希将与规范输入集不同，拒绝出指纹：\n  "
                         + "\n  ".join(missing)
                         + "\n（重跑 ffcalc.capture，确认 map 声明的 defective 字与 SBFE 位都读到了）")
    return entries


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dump", help="ffcalc.capture + mergeff 产出的快照 json（就地更新）")
    ap.add_argument("--bdf", default=None, help="顺手把 BDF 写进快照，便于事后追溯")
    ap.add_argument("--ffcalc", default=None, help="ffcalc 仓库根目录（含 ffcalc/ 包）")
    ap.add_argument("--source", default="ffcalc.capture(sysfs resource0)",
                    help="采集通道，写进 source 字段")
    ap.add_argument("--check", action="store_true", help="只核对已写入的指纹是否与重算一致")
    args = ap.parse_args()

    path = Path(args.dump)
    dump = json.loads(path.read_text(encoding="utf-8"))
    ingest = load_ffcalc(args.ffcalc)
    mmap = ingest.load_map()

    entries = build_entries(dump, mmap)
    fp = ingest.fingerprint(entries)          # 算法来自 ffcalc，本工具不实现哈希
    rows = dump.get("ff_rows") or {}
    print(f"指纹输入: {len(entries)} 项（map 声明 defective {len(mmap['defective'])} 字 + SBFE）")
    print(f"ffcalc.ingest.fingerprint = {fp}")
    print(f"ff_rows: {len(rows)} 条" + ("（未合并 512 行，不能出计划）" if len(rows) != 512 else ""))
    if len(rows) != 512:
        raise SystemExit("快照没有完整 512 行：先跑 ffcalc mergeff，再谈身份")

    if args.check:
        have = dump.get("fingerprint")
        if have != fp:
            print(f"FAIL: 快照里的 fingerprint={have} 与重算的 {fp} 不符")
            return 1
        print(f"OK: 快照指纹自证一致（{fp}）")
        return 0

    old = dump.get("fingerprint")
    if old and old != fp:
        raise SystemExit(f"快照已带 fingerprint={old}，与重算 {fp} 不符：这不是同一块 die，"
                         "或者中途有人烧过（SBFE 0→1 会改指纹）。停手核对，别继续")
    dump["fingerprint"] = fp
    dump["source"] = args.source
    if args.bdf:
        dump["bdf"] = args.bdf
    dump["identity"] = {
        "method": "ffcalc.ingest.fingerprint(defective.* + info.secured_burn_feature_enable)",
        "inputs": len(entries),
        "uuid": None,
        "uuid_channel": "无：判决机未安装 nvidia 驱动（无 nvidia-smi / nvidia.ko），"
                        "ingest 的 UUID 通道取不到",
        "meaning": "只证明烧前烧后同一块 die；同批次卡可相同，不替代 UUID 认卡",
        "still_required": ["512 行两次独立读逐字节一致",
                           "ga100vfi.burn crosscheck 现场逐行回读目标行"],
    }
    path.write_text(json.dumps(dump, indent=1, sort_keys=True) + "\n", encoding="utf-8",
                    newline="\n")
    print(f"写入 {path}: fingerprint={fp} source={args.source}")
    print("下一步：ffcalc gen（计划会带上 fingerprint）→ sim → verify → crosscheck")
    return 0


if __name__ == "__main__":
    sys.exit(main())
