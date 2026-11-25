#!/usr/bin/env python3
"""Cross-platform offline regression suite. Never opens serial, network or PCIe."""
from pathlib import Path
import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(argv, *, env=None):
    result = subprocess.run([str(x) for x in argv], cwd=ROOT, env=env)
    return result.returncode == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--python-only', action='store_true', help='run only host Python tests')
    ap.add_argument('--sanitizers', action='store_true', help='enable GCC/Clang address and undefined-behavior checks')
    args = ap.parse_args()
    ok = run([sys.executable, '-m', 'unittest', 'discover', '-s', 'host/tests', '-v'])
    if args.python_only:
        return 0 if ok else 1
    cc = shlex.split(os.environ['CC']) if os.environ.get('CC') else []
    if not cc:
        compiler = next((shutil.which(x) for x in ('cc','gcc','clang') if shutil.which(x)), None)
        if compiler: cc = [compiler]
    if not cc:
        print('C compiler required; install GCC/Clang, or explicitly use --python-only.', file=sys.stderr)
        return 1
    suites = [
        ('planner', ['test/test_glitch_plan.c','main/glitch_plan.c'], [], []),
        ('protocol', ['test/test_protocol.c','main/protocol.c'], [], []),
        ('peer', ['test/test_peer.c','main/peer.c'], ['test/stubs_campaign'], []),
        ('campaign', ['test/test_campaign.c','main/campaign.c','main/oracle.c','main/glitch_plan.c'],
         ['test/stubs_campaign'], [] if os.name == 'nt' else ['-DCAMPAIGN_TEST_THREADS','-pthread']),
        ('runtime', ['test/test_main.c','main/protocol.c','main/glitch_plan.c','main/campaign.c','main/oracle.c','main/peer.c'],
         ['test/stubs_runtime'], []),
        ('console', ['test/test_console.c','main/protocol.c'], ['test/stubs_runtime'], []),
        ('pmbus', ['test/test_pmbus.c','main/glitch_plan.c'], ['test/stubs_peripherals'], []),
        ('pmbus_high_bank', ['test/test_pmbus.c','main/glitch_plan.c'], ['test/stubs_peripherals'], ['-DTEST_PEX_HIGH_PIN']),
    ]
    suites.append(('http', ['test/test_net.c'], ['test/stubs_net'], []))
    build = ROOT/'build'/'tests'
    build.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    flags = ['-std=gnu11','-Wall','-Wextra','-Werror','-O1']
    if args.sanitizers:
        flags += ['-fsanitize=address,undefined','-fno-omit-frame-pointer','-g']
        # LeakSanitizer cannot run under ptrace/sandbox on some hosts. Tests
        # retain fake RTOS handles deliberately; address/UB checks stay enabled.
        environment.setdefault('ASAN_OPTIONS','detect_leaks=0')
    with tempfile.TemporaryDirectory(prefix='regression-', dir=build) as tmp:
        for name, sources, includes, extra in suites:
            print(f'[C] {name}', flush=True)
            exe = Path(tmp)/(name+('.exe' if os.name == 'nt' else ''))
            include_flags = [part for path in [*includes,'main'] for part in ('-I',path)]
            command = [*cc,*flags,*include_flags,*sources,*extra,'-o',exe]
            built = run(command, env=environment)
            ok = built and ok
            if built: ok = run([exe], env=environment) and ok
        node = shutil.which('node')
        if node:
            html = (ROOT/'main/webui.html').read_text(encoding='utf-8')
            for i, source in enumerate(re.findall(r'<script[^>]*>(.*?)</script>',html,re.S|re.I)):
                js = Path(tmp)/f'webui-{i}.js';js.write_text(source,encoding='utf-8')
                ok = run([node,'--check',js]) and ok
        else:
            print('Node unavailable: JavaScript syntax check skipped.', flush=True)
    ok = run([sys.executable,'tools/sync_arduino.py','--check']) and ok
    # Verify the checked-in generated web header without rewriting it.
    html = (ROOT/'main/webui.html').read_text(encoding='utf-8')
    import ast
    header = (ROOT/'main/webui.h').read_text(encoding='utf-8')
    literals = re.findall(r'^\s*("(?:[^"\\]|\\.)*")\s*;?\s*$',header,re.M)
    if ''.join(ast.literal_eval(x) for x in literals) != html:
        print('webui.h is stale; run python tools/gen_webui.py.',file=sys.stderr)
        ok = False
    print('OFFLINE REGRESSIONS PASSED' if ok else 'OFFLINE REGRESSIONS FAILED',flush=True)
    print('These tests use host models; ESP32 target compilation and board validation are separate gates.',flush=True)
    return 0 if ok else 1

if __name__ == '__main__':
    raise SystemExit(main())
