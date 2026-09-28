"""Build the independent desktop manager without requiring an installed kernel."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

PLATFORM = sys.platform
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'scripts'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hash():
    paths = [ROOT / name for name in ('Cargo.toml', 'Cargo.lock', 'build.rs', 'tauri.conf.json')]
    for folder in ('src', 'ui', 'icons'):
        paths.extend(p for p in (ROOT / folder).rglob('*') if p.is_file())
    h = hashlib.sha256()
    for path in sorted(paths):
        h.update(str(path.relative_to(ROOT)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--force-build', action='store_true')
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--safe', action='store_true')
    modes.add_argument('--normal', action='store_true')
    modes.add_argument('--manage', action='store_true')
    args = parser.parse_args()
    if PLATFORM not in ('linux', 'darwin'):
        raise RuntimeError('Use Start-DSH.ps1 on Windows (WSL backend).')
    version = json.loads((ROOT / 'tauri.conf.json').read_text())['version']
    target = ROOT / 'target'
    target.mkdir(exist_ok=True)
    with (target / '.unix-build.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binary = target / 'release/dsh-wsl-tauri'
        stamp_path = ROOT / f'ui-build-{PLATFORM}.json'
        try:
            stamp = json.loads(stamp_path.read_text())
        except (OSError, ValueError):
            stamp = {}
        source = source_hash()
        needs_build = (args.force_build or not binary.exists() or
                       stamp.get('shellVersion') != version or stamp.get('sourceSha256') != source or
                       stamp.get('binarySha256') != digest(binary))
        if needs_build:
            print(f'正在构建独立管理器 {version}，详细输出见 build-{PLATFORM}.log', flush=True)
            env = dict(os.environ, TAURI_CONFIG=json.dumps({'version': version}))
            with (ROOT / f'build-{PLATFORM}.log').open('w') as log:
                subprocess.run(['cargo', 'build', '--release', '--locked'], cwd=ROOT, env=env,
                               stdout=log, stderr=subprocess.STDOUT, check=True)
            if source_hash() != source:
                raise RuntimeError('构建期间版本或源码发生变化，请重新启动。')
            stamp = {'shellVersion': version, 'sourceSha256': source,
                     'binarySha256': digest(binary), 'builtAt': datetime.now(timezone.utc).isoformat()}
            temporary = stamp_path.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(stamp, indent=2) + '\n')
            temporary.replace(stamp_path)
            print(f'管理器 {version} 构建完成。', flush=True)
        else:
            print(f'管理器 {version}，源码一致，无需构建。', flush=True)
        if not args.build_only:
            env = dict(os.environ, DSH_TAURI_ROOT=str(ROOT))
            with (ROOT / f'desktop-{PLATFORM}.log').open('a') as log:
                subprocess.Popen([str(binary)] + (['--safe'] if args.safe else ['--normal'] if args.normal else ['--manage']), cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                             stdout=log, stderr=log, start_new_session=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        message = f'DSH 启动失败：{error}；构建日志：{ROOT / f"build-{PLATFORM}.log"}'
        print(message, file=sys.stderr)
        try:
            subprocess.run(['notify-send', 'DeepSeek Harness', message], check=False)
        except OSError:
            pass
        sys.exit(1)
