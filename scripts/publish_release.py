"""Build and verify a release before marking it ready for delivery."""
import argparse
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import subprocess
import sys
from build_release import build


def main():
    source=Path(__file__).resolve().parents[1]
    stamp=datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',default=str(source.parent/'DocQA-releases'/stamp))
    parser.add_argument('--live-config',help='Private configuration path; never include it in the release.')
    args=parser.parse_args()
    # Capture test output for the external report; keys are never command arguments.
    unit=subprocess.run([sys.executable,'-m','pytest','-q'],cwd=source,capture_output=True)
    print(unit.stdout.decode('utf-8',errors='replace'),flush=True)
    if unit.returncode:raise SystemExit('Unit tests failed; release not built.')
    result=build(source,args.out)
    destination=Path(args.out)
    (destination/'unit-tests.txt').write_bytes(unit.stdout+unit.stderr)
    command=[sys.executable,str(source/'scripts/verify_release.py'),'--archive',result['archive'],'--report',str(destination/'acceptance.json')]
    if args.live_config:command+=['--live-config',str(Path(args.live_config).resolve())]
    subprocess.run(command,cwd=source,check=True)
    subprocess.run([sys.executable,str(source/'scripts/verify_import_resilience.py'),'--archive',result['archive'],'--report',str(destination/'import-resilience.json')],cwd=source,check=True)
    browser_import=subprocess.run([sys.executable,str(Path(result['folder'])/'scripts/verify_import_ui.py')],cwd=result['folder'],capture_output=True)
    (destination/'import-ui.txt').write_bytes(browser_import.stdout+browser_import.stderr)
    if browser_import.returncode:
        raise SystemExit('Import browser regression failed; see import-ui.txt. Release not verified.')
    print('PASS: extracted-release browser import/retry regression.',flush=True)
    acceptance=json.loads((destination/'acceptance.json').read_text(encoding='utf-8'))
    if not acceptance.get('passed'):raise SystemExit('Acceptance failed; release not verified.')
    (destination/'VERIFIED.json').write_text(json.dumps({'sha256':result['sha256'],'acceptance_passed':True,'import_resilience_passed':True,'import_browser_passed':True,'real_models':acceptance.get('real_models',[])},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
