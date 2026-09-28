"""End-to-end npm/pnpm regression using a private loopback fixture registry.

No installed Harness is needed; no user plugin or service is modified.
"""
import base64
import hashlib
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tarfile
import tempfile
import threading
import urllib.parse
from maintenance import Maintenance, atomic_json, read_json, OFFICIAL, CORE

NAME = '@dsh-shell-test/plugin'


def archive(version):
    data = {'name': NAME, 'version': version, 'dsh': {'bundle': {'patch': './cordis.patch.yml'}},
            'peerDependencies': {CORE: '^0.1.5-rc.1' if version == '1.0.0' else '^0.1.7-rc.1'},
            'peerDependenciesMeta': {CORE: {'optional': True}}, 'engines': {'node': '>=20'},
            'scripts': {'postinstall': "node -e \"require('fs').writeFileSync('postinstall-ran','bad')\""}}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w:gz') as tar:
        for name, text in [('package.json', json.dumps(data)), ('cordis.patch.yml', '[]\n')]:
            raw = text.encode(); info = tarfile.TarInfo('package/' + name); info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return data, output.getvalue()


PACKAGES = {v: archive(v) for v in ('1.0.0', '1.1.0')}


class Registry(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def do_GET(self):
        path = urllib.parse.unquote(self.path.split('?', 1)[0])
        if path.startswith('/archives/'):
            version = path.removeprefix('/archives/').removesuffix('.tgz')
            if version not in PACKAGES:
                self.send_error(404); return
            body = PACKAGES[version][1]
            content = 'application/octet-stream'
        elif path == '/' + NAME:
            versions = {}
            for version, (data, raw) in PACKAGES.items():
                versions[version] = dict(data, dist={'tarball': f'http://127.0.0.1:{self.server.server_port}/archives/{version}.tgz',
                    'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(raw).digest()).decode()})
            body = json.dumps({'name':NAME,'dist-tags':{'latest':'1.1.0'},'versions':versions}).encode()
            content = 'application/json'
        else:
            self.send_error(404); return
        self.send_response(200);self.send_header('Content-Type',content);self.send_header('Content-Length',str(len(body)))
        self.end_headers();self.wfile.write(body)


def main():
    server = ThreadingHTTPServer(('127.0.0.1', 0), Registry)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='dsh-plugin-test-') as raw:
            root = Path(raw)
            manager = Maintenance({'home': str(root), 'port': 13089, 'core_executable': str(root/'no-kernel')})
            manager.env.update(npm_config_registry=f'http://127.0.0.1:{server.server_port}',
                               npm_config_cache=str(root/'cache'), npm_config_store_dir=str(root/'store'),
                               NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost')
            profile = root/'.dsh/profiles/web'
            atomic_json(profile/'package.json',{'private':True,'dependencies':{},'dsh':{'profile':{'bundles':OFFICIAL}}})
            (profile/'.npmrc').write_text(f'registry=http://127.0.0.1:{server.server_port}\nupdate-notifier=false\nstore-dir={root / "store"}\n')
            assert manager.inventory()['core'] is None
            assert manager.versions(NAME)['versions'] == ['1.1.0','1.0.0']
            assert manager.versions(NAME)['tags']['latest'] == '1.1.0'
            info = manager.dispatch({'action':'version_info','package':NAME,'version':'1.1.0'})
            assert info['coreStatus'] == 'unknown'
            assert info['checks'][0]['range'] == '^0.1.7-rc.1'
            assert info['checks'][0]['name'] == CORE
            assert info['engines'][0]['status'] == 'match'
            for version in ('1.0.0','1.1.0','1.0.0'):
                manager.dispatch({'action':'plugin_install','profile':'web','package':NAME,'version':version})
                assert read_json(profile/'package.json')['dependencies'][NAME] == version
                assert read_json(profile/'node_modules'/NAME/'package.json')['version'] == version
                assert NAME in read_json(profile/'package.json')['dsh']['profile']['bundles']
                assert not list(root.rglob('postinstall-ran'))
            manager.dispatch({'action':'plugin_toggle','profile':'web','package':NAME,'enabled':False})
            assert NAME not in read_json(profile/'package.json')['dsh']['profile']['bundles']
            manager.dispatch({'action':'plugin_remove','profile':'web','package':NAME})
            assert NAME not in read_json(profile/'package.json').get('dependencies',{})
            print('PASS: no-kernel versions, tags and exact-version compatibility declarations; plugin install, upgrade, downgrade, disable and remove; package scripts not executed')
    finally:
        server.shutdown();server.server_close()


if __name__ == '__main__': main()
