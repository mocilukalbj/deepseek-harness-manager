"""Kernel-independent maintenance for the desktop shell. Standard library only.

JSON on stdin/stdout; commands never import a Harness or third-party module.
Only the local Tauri management window may call this helper.
"""
import collections
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid

from backend import authenticate, candidate_urls, read_log, resolve

CORE = '@deepseek-ai/dsh'
OFFICIAL = ['@deepseek-ai/dsh-base', '@deepseek-ai/dsh-web-app']
VERSION = re.compile(r'\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z')
PACKAGE = re.compile(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*\Z')
PROFILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z')


def exact_version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError('请选择精确版本号，例如 0.1.5-rc.1；不接受 latest 或版本范围。')
    return value


def package_name(value):
    if not isinstance(value, str) or len(value) > 214 or not PACKAGE.fullmatch(value):
        raise ValueError('请输入有效的 npm 包名。')
    return value


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def redact(text):
    return re.sub(r'(?i)(token=|_authToken[=:]\s*)[^\s&]+', r'\1[REDACTED]', text)


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(.3)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def process_identity(pid):
    """Never signal a reused PID. Linux start ticks, otherwise ps start time."""
    try:
        stat = Path(f'/proc/{pid}/stat')
        if stat.exists():
            return stat.read_text().rsplit(')', 1)[1].split()[19]
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='],
                                capture_output=True, text=True, timeout=3)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


class Maintenance:
    def __init__(self, config):
        self.config = config
        self.home = Path(config['home']).expanduser().resolve()
        self.dsh_home = Path(config.get('dsh_home', self.home / '.dsh')).expanduser().resolve()
        self.state = Path(config.get('management_home', self.home / '.local/share/dsh-tauri')).expanduser().resolve()
        self.port = int(config['port'])
        self.safe_port = int(config.get('safe_port', self.port + 1))
        if not 1024 <= self.port <= 65535 or not 1024 <= self.safe_port <= 65535 or self.port == self.safe_port:
            raise ValueError('普通模式与安全模式需要不同的端口（1024–65535）。')
        self.env = dict(os.environ, HOME=str(self.home))
        self.env['PATH'] = os.pathsep.join([str(self.home / '.npm-global/bin'), str(self.home / '.local/bin'), self.env.get('PATH', '')])
        # Do not inherit a parent Harness profile, patch or snapshot selection.
        for key in list(self.env):
            if key.startswith('DSH_'):
                self.env.pop(key)
        self.log = self.state / 'maintenance.log'

    def run(self, command, cwd=None, timeout=900, capture=False, env=None):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        if capture:
            result = subprocess.run(command, cwd=cwd, env=env or self.env, capture_output=True,
                                    text=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError(redact(result.stderr[-2000:] or result.stdout[-2000:] or '命令执行失败'))
            return result.stdout
        with self.log.open('a') as output:
            os.chmod(self.log, 0o600)
            output.write('\n' + time.strftime('%H:%M:%S') + ' · ' + ' '.join(map(str, command)) + '\n')
            output.flush()
            child = subprocess.Popen(command, cwd=cwd, env=env or self.env, stdout=output,
                                     stderr=subprocess.STDOUT, start_new_session=True)
            try:
                code = child.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
                raise RuntimeError('管理操作超时，已停止此次操作的子进程；可查看日志后重试。')
        if code:
            raise RuntimeError(redact(read_log(self.log)[-3000:]))
        return ''

    def tool(self, name):
        path = shutil.which(name, path=self.env['PATH'])
        if not path and (self.home / '.nvm/nvm.sh').is_file():
            script = 'export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use default >/dev/null 2>&1 || exit 1; printf "%s" "$PATH"'
            result = subprocess.run(['bash', '-c', script], env=self.env,
                                    capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                self.env['PATH'] = result.stdout
                path = shutil.which(name, path=self.env['PATH'])
        if not path:
            raise RuntimeError(f'未找到 {name}。请先安装它并加入 PATH；管理页面仍可使用。')
        return path

    @contextlib.contextmanager
    def lock(self):
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / 'maintenance.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('另一个管理操作正在进行，请等待它完成。')
            yield

    def activation_path(self):
        state = read_json(self.state / 'selection.json', {})
        raw = self.config.get('core_executable') or state.get('activationPath')
        if not raw:
            raw = shutil.which('dsh', path=self.env['PATH'])
        if not raw:
            try:
                raw = self.tool('dsh')
            except RuntimeError:
                pass
        if not raw:
            prefix = self.run([self.tool('npm'), 'prefix', '-g'], timeout=30, capture=True).strip()
            raw = str(Path(prefix) / 'bin/dsh')
        path = Path(raw).expanduser()
        if not path.is_absolute():
            raise ValueError('core_executable 必须是绝对路径。')
        return path

    @staticmethod
    def core_at(entry):
        entry = Path(entry).resolve(strict=True)
        for root in list(entry.parents)[:5]:
            p = root / 'package.json'
            if p.exists():
                data = read_json(p)
                if data.get('name') == CORE:
                    return {'version': exact_version(data['version']), 'entry': str(entry), 'root': str(root)}
        raise RuntimeError('dsh 入口未指向有效的 DeepSeek Harness 安装。')

    def current_core(self):
        return self.core_at(self.activation_path())

    def profile_path(self, name):
        if not isinstance(name, str) or not PROFILE.fullmatch(name) or name == 'desktop':
            raise ValueError('无效或保留的 profile 名称。')
        parent = self.dsh_home / 'profiles'
        path = parent / name
        if path.is_symlink() or path.resolve().parent != parent.resolve():
            raise ValueError('不接受指向其他目录的 profile。')
        return path

    def inventory(self):
        result = {'core': None, 'coreError': None, 'cores': [], 'profiles': [],
                  'normalRunning': listening(self.port), 'safeRunning': self.safe_running(),
                  'safePort': self.safe_port, 'dshHome': str(self.dsh_home),
                  'logTail': redact(read_log(self.log)[-2400:])}
        try:
            result['core'] = self.current_core()
            result['cores'].append(dict(result['core'], current=True))
        except Exception as error:
            result['coreError'] = str(error)
        entries = []
        try:
            old = read_json(self.state / 'selection.json', {}).get('originalEntry')
            if old:
                entries.append(Path(old))
        except (ValueError, OSError):
            pass
        for p in (self.state / 'kernels').glob('*/ready.json'):
            try:
                entries.append(Path(read_json(p)['entry']))
            except (ValueError, KeyError, OSError):
                continue
        for entry in entries:
            try:
                core = self.core_at(entry)
                if not any(c['entry'] == core['entry'] for c in result['cores']):
                    result['cores'].append(dict(core, current=False))
            except Exception:
                pass
        for p in sorted((self.dsh_home / 'profiles').glob('*/package.json')):
            profile = {'name': p.parent.name, 'plugins': [], 'error': None}
            try:
                self.profile_path(p.parent.name)
                data = read_json(p)
                bundles = data.get('dsh', {}).get('profile', {}).get('bundles', [])
                deps = data.get('dependencies', {})
                for name in dict.fromkeys([*deps, *bundles]):
                    package_name(name)
                    if name.startswith('@deepseek-ai/dsh-'):
                        continue
                    try:
                        pkg = read_json(p.parent / 'node_modules' / name / 'package.json')
                        peers = {k: v for k, v in pkg.get('peerDependencies', {}).items()
                                 if k == CORE or k.startswith('@deepseek-ai/dsh-')}
                        installed = pkg.get('version')
                    except (OSError, ValueError):
                        installed, peers = None, {}
                    spec = deps.get(name, '')
                    profile['plugins'].append({'name': name, 'spec': spec, 'version': installed,
                                               'enabled': name in bundles, 'peers': peers,
                                               'local': spec.startswith(('link:', 'file:', 'workspace:'))})
            except Exception as error:
                profile['error'] = str(error)
            result['profiles'].append(profile)
        return result

    def versions(self, name):
        name = package_name(name)
        data = self.registry_view(name, 'versions', 'dist-tags')
        values = data.get('versions', []) if isinstance(data, dict) else data
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            raise RuntimeError('npm 返回的版本列表格式无效，请重试。')
        values = list(dict.fromkeys(v for v in reversed(values)
                                    if isinstance(v, str) and VERSION.fullmatch(v)))
        if not values:
            raise RuntimeError('此包没有可用的已发布版本，请检查包名和 npm registry 配置。')
        return {'package': name, 'versions': values,
                'tags': data.get('dist-tags', {}) if isinstance(data, dict) else {}}

    def registry_view(self, spec, *fields):
        try:
            raw = self.run([self.tool('npm'), 'view', spec, *fields, '--json',
                            '--fetch-retries=1', '--fetch-timeout=15000'], timeout=45, capture=True)
            return json.loads(raw)
        except subprocess.TimeoutExpired:
            raise RuntimeError('版本查询超时，请检查网络和 npm registry 后重试。') from None
        except json.JSONDecodeError:
            raise RuntimeError('npm 未返回有效的版本信息，请检查包名和 registry 配置。') from None

    def check_ranges(self, rows):
        """Use npm's semver, without importing the kernel or any plugin code."""
        if not rows:
            return
        script = r'''
const fs = require('fs');
const { createRequire } = require('module');
const semver = createRequire(fs.realpathSync(process.argv[1]))('semver');
const rows = JSON.parse(process.argv[2]);
console.log(JSON.stringify(rows.map(row => {
  let range = row.range;
  const options = { includePrerelease: row.kind === 'dsh' };
  if (row.kind === 'dsh' && ['workspace:*', 'workspace:^', 'workspace:~'].includes(range)) range = '*';
  if (typeof range !== 'string' || semver.validRange(range, options) === null) return 'invalid';
  if (!row.version || !semver.valid(row.version)) return 'unknown';
  return semver.satisfies(row.version, range, options) ? 'match' : 'mismatch';
})));
'''
        try:
            env = {k: v for k, v in self.env.items() if k not in ('NODE_OPTIONS', 'NODE_PATH')}
            statuses = json.loads(self.run([self.tool('node'), '-e', script, self.tool('npm'),
                                           json.dumps(rows)], timeout=15, capture=True, env=env))
            for row, status in zip(rows, statuses):
                row['status'] = status
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
            for row in rows:
                row.update(status='unknown', reason='无法计算版本范围：' + redact(str(error))[:300])

    @staticmethod
    def compatibility(rows):
        rows = [row for row in rows if row['kind'] == 'dsh']
        if not rows:
            return 'undeclared'
        for status in ('invalid', 'mismatch', 'unknown'):
            if any(row['status'] == status for row in rows):
                return status
        return 'match'

    @staticmethod
    def peer_rows(peers, version, meta=None):
        return [{'name': name, 'range': value, 'version': version if name == CORE or name.startswith('@deepseek-ai/dsh-') else None,
                 'kind': 'dsh' if name == CORE or name.startswith('@deepseek-ai/dsh-') else 'other',
                 'optional': bool((meta or {}).get(name, {}).get('optional')), 'status': 'unknown'}
                for name, value in peers.items()]

    def version_info(self, name, version):
        name, version = package_name(name), exact_version(version)
        data = self.registry_view(f'{name}@{version}')
        if not isinstance(data, dict) or data.get('name') != name or data.get('version') != version:
            raise RuntimeError('npm 返回的包或版本与所选版本不符，请重新获取。')
        state = self.inventory()
        current = (state['core'] or {}).get('version')
        target = version if name == CORE else current
        try:
            node_version = self.run([self.tool('node'), '--version'], timeout=10, capture=True).strip().lstrip('v')
        except (OSError, RuntimeError, subprocess.SubprocessError):
            node_version = None
        peers = data.get('peerDependencies') or {}
        engines = data.get('engines') or {}
        checks = self.peer_rows(peers, target, data.get('peerDependenciesMeta'))
        engine_rows = [{'name': key, 'range': value, 'version': node_version if key == 'node' else None,
                        'kind': 'engine', 'status': 'unknown'} for key, value in engines.items()]
        installed = []
        if name == CORE:
            for profile in state['profiles']:
                for plugin in profile['plugins']:
                    rows = self.peer_rows(plugin['peers'], target)
                    installed.append({'profile': profile['name'], 'name': plugin['name'],
                                      'version': plugin['version'], 'enabled': plugin['enabled'], 'checks': rows})
        all_rows = [r for r in checks if r['kind'] == 'dsh'] + [r for r in engine_rows if r['name'] == 'node']
        all_rows += [r for plugin in installed for r in plugin['checks']]
        self.check_ranges(all_rows)
        for plugin in installed:
            plugin['coreStatus'] = self.compatibility(plugin['checks'])
        return {'package': name, 'version': version, 'description': data.get('description', ''),
                'currentCore': current, 'targetCore': target, 'nodeVersion': node_version,
                'checks': checks, 'engines': engine_rows, 'coreStatus': self.compatibility(checks),
                'installedPlugins': installed, 'profileErrors': [{'profile': p['name'], 'error': p['error']}
                    for p in state['profiles'] if p['error']],
                'isBundle': bool(data.get('dsh', {}).get('bundle')), 'deprecated': data.get('deprecated')}

    def stop_normal(self):
        unit = self.config.get('journal_unit')
        if unit:
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9@_.-]*\.service', unit):
                raise ValueError('无效的 systemd 用户服务名。')
            self.run(['systemctl', '--user', 'stop', unit], timeout=120)
        elif listening(self.port):
            launcher = self.config.get('launcher')
            if not launcher:
                raise RuntimeError('后端正在运行且未配置停止方式。请先停止后端，再执行版本操作。')
            launcher = Path(launcher).expanduser()
            if not launcher.is_absolute():
                launcher = self.home / launcher
            self.run(['bash', str(launcher), 'stop'], timeout=90)
        if listening(self.port):
            raise RuntimeError('后端端口仍被占用，未修改安装。请先停止该进程。')

    def safe_record(self):
        return read_json(self.state / 'safe-process.json', {})

    def safe_running(self):
        try:
            record = self.safe_record()
            return bool(record.get('identity') and process_identity(record['pid']) == record['identity'])
        except (OSError, ValueError, KeyError):
            return False

    def stop_safe(self):
        if self.safe_running():
            record = self.safe_record()
            pid = record['pid']
            if os.getpgid(pid) != pid:
                raise RuntimeError('安全模式进程组已变化，拒绝停止未知进程。')
            os.killpg(pid, signal.SIGTERM)
            for _ in range(50):
                if not self.safe_running():
                    break
                time.sleep(.1)
            if self.safe_running():
                os.killpg(pid, signal.SIGKILL)
        (self.state / 'safe-process.json').unlink(missing_ok=True)

    def open_safe(self):
        core = self.current_core()
        if self.safe_running():
            record = self.safe_record()
            for url in candidate_urls(read_log(Path(record['log'])), self.safe_port):
                try:
                    authenticate(url)
                    return {'url': url, 'version': record['version'], 'mode': 'safe'}
                except Exception:
                    continue
            raise RuntimeError('安全模式正在启动或已失去响应，请先停止安全模式再重试。')
        if listening(self.safe_port):
            raise RuntimeError('安全模式端口已被其他服务占用，未接管该进程。')
        runs = self.state / 'safe-runs'
        runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        clean = Path(tempfile.mkdtemp(prefix='session-', dir=runs))
        profile = clean / 'profiles/web'
        atomic_json(profile / 'package.json', {'name': 'dsh-safe-web', 'private': True, 'dependencies': {},
                    'dsh': {'profile': {'bundles': OFFICIAL, 'patchReload': 'startup'}}})
        env = dict(self.env, DSH_HOME=str(clean), DSH_HOST='127.0.0.1', DSH_PORT=str(self.safe_port))
        env.pop('NODE_OPTIONS', None)
        env.pop('NODE_PATH', None)
        log = clean / 'backend.log'
        with log.open('w') as stream:
            os.chmod(log, 0o600)
            child = subprocess.Popen([self.tool('node'), core['entry'], 'web', '--no-open', '--host', '127.0.0.1', '--port', str(self.safe_port)],
                                     cwd=clean, env=env, stdin=subprocess.DEVNULL,
                                     stdout=stream, stderr=stream, start_new_session=True)
        identity = process_identity(child.pid)
        if not identity:
            child.terminate()
            raise RuntimeError('无法记录安全模式进程身份。')
        atomic_json(self.state / 'safe-process.json', {'pid': child.pid, 'identity': identity,
                    'log': str(log), 'home': str(clean), 'version': core['version']})
        try:
            for _ in range(45):
                if child.poll() is not None:
                    raise RuntimeError('安全模式启动失败：' + redact(read_log(log)[-2200:]))
                for url in candidate_urls(read_log(log), self.safe_port):
                    try:
                        authenticate(url)
                        return {'url': url, 'version': core['version'], 'mode': 'safe'}
                    except Exception:
                        pass
                time.sleep(1)
            raise RuntimeError('安全模式启动超时。可在版本管理中切换内核。')
        except Exception:
            self.stop_safe()
            raise

    def open_normal(self):
        core = self.current_core()
        result = resolve(self.home, self.port, self.config.get('log_glob'),
                         self.config.get('journal_unit'), self.config.get('launcher'))
        return dict(result, version=core['version'], mode='normal')

    def activate(self, core):
        path = self.activation_path()
        if path.exists() and not path.is_symlink():
            raise RuntimeError('dsh 入口是自定义普通文件，未覆盖。请把 core_executable 配置为可管理的符号链接。')
        selection = read_json(self.state / 'selection.json', {})
        if not selection.get('originalEntry'):
            try:
                selection['originalEntry'] = self.current_core()['entry']
            except Exception:
                pass
        self.stop_normal()
        self.stop_safe()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name('.dsh-switch-' + uuid.uuid4().hex)
        try:
            temporary.symlink_to(core['entry'])
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        selection.update(activationPath=str(path), version=core['version'])
        atomic_json(self.state / 'selection.json', selection)
        return {'message': f"已切换至 {core['version']}。后端保持停止，可选择普通或安全模式启动。"}

    def normalize_core(self, stage, version):
        """Older npm releases have floating internal deps; pin and deduplicate."""
        manifest = read_json(stage / 'package.json')
        pins = {CORE: version}
        exact_peers = collections.defaultdict(set)
        modules = stage / 'node_modules'
        for root, dirs, files in os.walk(modules):
            # Package manifests only. No source imports or user plugin reads.
            if 'package.json' not in files:
                continue
            path = Path(root) / 'package.json'
            try:
                data = read_json(path)
            except (ValueError, OSError):
                continue
            name = data.get('name', '')
            if name.startswith('@deepseek-ai/dsh-'):
                pins[name] = version
            if name == CORE or name.startswith('@deepseek-ai/dsh-'):
                for section in ('dependencies', 'peerDependencies', 'optionalDependencies'):
                    for dependency in data.get(section, {}):
                        if dependency.startswith('@deepseek-ai/dsh-'):
                            pins[dependency] = version
                for peer, spec in data.get('peerDependencies', {}).items():
                    if peer.startswith('@deepseek-ai/') and isinstance(spec, str) and VERSION.fullmatch(spec):
                        exact_peers[peer].add(spec)
        for name, values in exact_peers.items():
            if name in pins:
                continue
            if len(values) != 1:
                raise RuntimeError(f'内核依赖对 {name} 有冲突的版本要求，未切换当前内核。')
            pins[name] = next(iter(values))
        manifest['overrides'] = pins
        atomic_json(stage / 'package.json', manifest)
        npm = self.tool('npm')
        args = ['--prefix', str(stage), '--prefer-offline', '--no-audit', '--no-fund']
        self.run([npm, 'install', *args])
        self.run([npm, 'dedupe', *args])
        for root, dirs, files in os.walk(modules):
            if 'package.json' not in files:
                continue
            try:
                data = read_json(Path(root) / 'package.json')
            except (OSError, ValueError):
                continue
            if data.get('name', '').startswith('@deepseek-ai/dsh-') and data.get('version') != version:
                raise RuntimeError('内核内部版本未能统一，未切换当前内核。')

    def probe_core(self, entry, directory):
        """Boot the candidate in a disposable home before changing the entry."""
        with tempfile.TemporaryDirectory(prefix='.probe-', dir=directory) as raw:
            clean = Path(raw)
            atomic_json(clean / 'profiles/web/package.json', {
                'name': 'dsh-core-probe', 'private': True, 'dependencies': {},
                'dsh': {'profile': {'bundles': OFFICIAL, 'patchReload': 'startup'}}})
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
            env = dict(self.env, DSH_HOME=str(clean), DSH_HOST='127.0.0.1', DSH_PORT=str(port))
            env.pop('NODE_OPTIONS', None)
            env.pop('NODE_PATH', None)
            log = clean / 'probe.log'
            with log.open('w') as stream:
                child = subprocess.Popen([self.tool('node'), str(entry), 'web', '--no-open', '--host', '127.0.0.1', '--port', str(port)],
                                         cwd=clean, env=env, stdin=subprocess.DEVNULL,
                                         stdout=stream, stderr=stream, start_new_session=True)
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        break
                    for url in candidate_urls(read_log(log), port):
                        try:
                            authenticate(url)
                            return
                        except Exception:
                            pass
                    time.sleep(1)
                raise RuntimeError('候选内核启动验证失败，未切换当前版本：' + redact(read_log(log)[-2400:]))
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait(timeout=5)

    def install_core(self, version):
        version = exact_version(version)
        # Stage everything before touching the active executable or service.
        kernels = self.state / 'kernels'
        kernels.mkdir(parents=True, exist_ok=True, mode=0o700)
        stage = Path(tempfile.mkdtemp(prefix='.install-', dir=kernels))
        try:
            atomic_json(stage / 'package.json', {'name': 'dsh-shell-runtime', 'private': True,
                                                'dependencies': {CORE: version}})
            self.run([self.tool('npm'), 'install', '--prefix', str(stage), '--legacy-peer-deps', '--prefer-offline', '--no-audit', '--no-fund'])
            self.normalize_core(stage, version)
            entry = stage / 'node_modules/@deepseek-ai/dsh/lib/bin.js'
            value = self.run([self.tool('node'), str(entry), '--version'], timeout=30, capture=True).strip()
            if value != version:
                raise RuntimeError('安装后的内核版本校验失败，未切换。')
            self.probe_core(entry, stage)
            destination = kernels / (version + '-' + uuid.uuid4().hex[:8])
            stage.rename(destination)
            entry = destination / 'node_modules/@deepseek-ai/dsh/lib/bin.js'
            atomic_json(destination / 'ready.json', {'entry': str(entry), 'version': version})
            return self.activate(self.core_at(entry))
        finally:
            if stage.exists():
                shutil.rmtree(stage)

    def switch_core(self, version):
        exact_version(version)
        for core in self.inventory()['cores']:
            if core['version'] == version:
                return self.activate(core)
        raise RuntimeError('本机没有该版本，请先安装。')

    def plugin_action(self, request):
        name = package_name(request.get('package'))
        if name == CORE or name.startswith('@deepseek-ai/dsh-'):
            raise ValueError('官方内核组件随内核统一管理，不能在此删除或覆盖。')
        path = self.profile_path(request.get('profile', 'web'))
        file = path / 'package.json'
        data = read_json(file)
        settings = data.setdefault('dsh', {}).setdefault('profile', {})
        bundles = settings.setdefault('bundles', [])
        deps = data.get('dependencies', {})
        action = request['action']
        if action != 'plugin_install' and name not in deps and name not in bundles:
            raise ValueError('该 profile 中不存在此插件。')
        spec = deps.get(name, '')
        if action == 'plugin_install':
            version = exact_version(request.get('version'))
            if spec.startswith(('link:', 'file:', 'workspace:')):
                raise ValueError('本地源码插件不能用 npm 版本覆盖；可以停用或移除它。')
            metadata = json.loads(self.run([self.tool('npm'), 'view', f'{name}@{version}', '--json'], timeout=60, capture=True))
            if metadata.get('name') != name or metadata.get('version') != version or not metadata.get('dsh', {}).get('bundle'):
                raise ValueError('此版本未声明有效的 Harness bundle，未安装。')
        if action == 'plugin_toggle' and not isinstance(request.get('enabled'), bool):
            raise ValueError('enabled 必须为布尔值。')
        # Do not let profile HMR load half-written dependencies.
        self.stop_normal()
        if action == 'plugin_remove' and name not in deps:
            bundles[:] = [n for n in bundles if n != name]
            atomic_json(file, data)
            return {'message': '已移除失效的插件 bundle 声明。'}
        if action == 'plugin_toggle':
            if request['enabled'] and name not in bundles:
                installed = read_json(path / 'node_modules' / name / 'package.json')
                if not installed.get('dsh', {}).get('bundle'):
                    raise ValueError('此包未声明 Harness bundle。')
                bundles.append(name)
            elif not request['enabled']:
                bundles[:] = [n for n in bundles if n != name]
            atomic_json(file, data)
        else:
            pnpm = self.tool('pnpm')
            if action == 'plugin_install':
                args = ['add', '--save-exact', f'{name}@{version}', '--ignore-scripts']
            else:
                args = ['remove', name, '--config.ignore-scripts=true']
            self.run([pnpm, '--dir', str(path), *args])
            data = read_json(file)
            bundles = data.setdefault('dsh', {}).setdefault('profile', {}).setdefault('bundles', [])
            if action == 'plugin_remove':
                bundles[:] = [n for n in bundles if n != name]
            else:
                installed = read_json(path / 'node_modules' / name / 'package.json')
                if installed.get('dsh', {}).get('bundle') and name not in bundles:
                    bundles.append(name)
            atomic_json(file, data)
        return {'message': '插件配置已更新。普通后端保持停止，选择启动模式后生效。'}

    def dispatch(self, request):
        action = request.get('action')
        if action == 'status':
            return self.inventory()
        if action == 'versions':
            return self.versions(request.get('package', CORE))
        if action == 'version_info':
            return self.version_info(request.get('package', CORE), request.get('version'))
        with self.lock():
            if action == 'open_normal':
                return self.open_normal()
            if action == 'open_safe':
                return self.open_safe()
            if action == 'stop_safe':
                self.stop_safe()
                return {'message': '安全模式已停止。'}
            if action == 'stop_normal':
                self.stop_normal()
                return {'message': '普通后端已停止。'}
            if action == 'install_core':
                return self.install_core(request.get('version'))
            if action == 'switch_core':
                return self.switch_core(request.get('version'))
            if action in ('plugin_install', 'plugin_remove', 'plugin_toggle'):
                return self.plugin_action(request)
            raise ValueError('不支持的管理操作。')


if __name__ == '__main__':
    try:
        payload = json.loads(sys.stdin.read(65536))
        result = Maintenance(payload['config']).dispatch(payload['request'])
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': redact(str(error))}, ensure_ascii=False))
        sys.exit(1)
