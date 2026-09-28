import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import maintenance as m


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'home': str(self.root), 'port': 3080, 'management_home': str(self.root/'manager')}
        self.manager = m.Maintenance(self.config)
        self.profile = self.root / '.dsh/profiles/web'
        m.atomic_json(self.profile/'package.json', {'dependencies': {'example-plugin':'1.2.3'},
                      'dsh': {'profile': {'bundles': [*m.OFFICIAL, 'example-plugin']}}})
        m.atomic_json(self.profile/'node_modules/example-plugin/package.json',
                      {'name':'example-plugin', 'version':'1.2.3', 'dsh':{'bundle':{'patch':'cordis.patch.yml'}}})

    def test_inventory_survives_missing_kernel_and_npm(self):
        with patch.object(self.manager, 'current_core', side_effect=RuntimeError('not installed')), patch.object(m, 'listening', return_value=False):
            data = self.manager.inventory()
        self.assertIsNone(data['core'])
        self.assertEqual(data['profiles'][0]['plugins'][0]['version'], '1.2.3')

    def test_inventory_reports_corrupt_profile_without_losing_other_status(self):
        (self.profile/'package.json').write_text('{broken')
        with patch.object(self.manager, 'current_core', side_effect=RuntimeError('missing')), patch.object(m, 'listening', return_value=False):
            self.assertTrue(self.manager.inventory()['profiles'][0]['error'])

    def test_rejects_traversal_and_shell_arguments(self):
        for value in ('../web', '/tmp/web', 'a/b', '--help', 'desktop'):
            with self.assertRaises(ValueError): self.manager.profile_path(value)
        for value in ('--registry=evil', 'plugin;touch /tmp/x', '../plugin', 'file:/tmp/x', 'name@latest'):
            with self.assertRaises(ValueError): m.package_name(value)
        for value in ('latest', '^0.1.5', '../0.1.5', '0.1.5;echo x'):
            with self.assertRaises(ValueError): m.exact_version(value)
        self.assertEqual(m.exact_version('0.1.5-rc.1'), '0.1.5-rc.1')

    def test_profile_symlink_cannot_escape(self):
        (self.profile.parent/'outside').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): self.manager.profile_path('outside')

    def test_official_components_cannot_be_removed(self):
        with patch.object(self.manager, 'stop_normal') as stop:
            with self.assertRaises(ValueError):
                self.manager.plugin_action({'action':'plugin_remove', 'package':'@deepseek-ai/dsh-base', 'profile':'web'})
            stop.assert_not_called()

    def test_disabling_plugin_requires_no_kernel_and_keeps_dependency(self):
        with patch.object(self.manager, 'stop_normal') as stop, patch.object(self.manager, 'current_core', side_effect=AssertionError('must not load kernel')):
            self.manager.plugin_action({'action':'plugin_toggle', 'package':'example-plugin', 'profile':'web', 'enabled':False})
        data = m.read_json(self.profile/'package.json')
        self.assertEqual(data['dependencies']['example-plugin'], '1.2.3')
        self.assertEqual(data['dsh']['profile']['bundles'], m.OFFICIAL)
        stop.assert_called_once()

    def test_local_plugin_cannot_be_overwritten_by_registry_version(self):
        data=m.read_json(self.profile/'package.json');data['dependencies']['example-plugin']='link:/source/plugin'
        m.atomic_json(self.profile/'package.json',data)
        with patch.object(self.manager, 'stop_normal') as stop:
            with self.assertRaises(ValueError):
                self.manager.plugin_action({'action':'plugin_install','package':'example-plugin','profile':'web','version':'2.0.0'})
            stop.assert_not_called()

    def test_failed_stop_does_not_change_profile(self):
        before=(self.profile/'package.json').read_bytes()
        with patch.object(self.manager, 'stop_normal', side_effect=RuntimeError('busy')):
            with self.assertRaises(RuntimeError):
                self.manager.plugin_action({'action':'plugin_toggle','package':'example-plugin','profile':'web','enabled':False})
        self.assertEqual((self.profile/'package.json').read_bytes(),before)

    def test_failure_during_download_does_not_stop_or_switch_running_core(self):
        with patch.object(self.manager,'run',side_effect=RuntimeError('download failed')), \
             patch.object(self.manager,'tool',return_value='npm'), \
             patch.object(self.manager,'activate') as activate, patch.object(self.manager,'stop_normal') as stop:
            with self.assertRaises(RuntimeError): self.manager.install_core('0.1.5-rc.1')
            activate.assert_not_called();stop.assert_not_called()
        self.assertEqual(list((self.manager.state/'kernels').iterdir()),[])

    def test_safe_mode_creates_isolated_home_and_ignores_custom_environment(self):
        old=m.read_json(self.profile/'package.json')
        (self.root/'.dsh/cordis.patch.yml').write_text('untrusted custom patch')
        with patch.dict(os.environ, {'DSH_SNAPSHOT':'bad','DSH_PROFILE':'bad','DSH_HOME':'bad','NODE_OPTIONS':'--require untrusted','NODE_PATH':'/untrusted'}):
            manager=m.Maintenance(self.config)
        child=MagicMock(pid=7654);child.poll.return_value=None
        with patch.object(manager,'current_core',return_value={'entry':'/core/bin.js','version':'0.1.5-rc.1'}), \
             patch.object(manager,'tool',return_value='/usr/bin/node'), \
             patch.object(m,'listening',return_value=False), patch.object(m,'process_identity',return_value='123'), \
             patch.object(m.subprocess,'Popen',return_value=child) as launch, \
             patch.object(m,'candidate_urls',return_value=['http://127.0.0.1:3081/?token=test']), \
             patch.object(m,'authenticate',return_value=True):
            result=manager.open_safe()
        env=launch.call_args.kwargs['env']; clean=Path(env['DSH_HOME'])
        self.assertNotEqual(clean,self.root/'.dsh')
        self.assertEqual(env['DSH_PORT'],'3081')
        self.assertEqual(launch.call_args.args[0][-4:], ['--host','127.0.0.1','--port','3081'])
        self.assertNotIn('DSH_SNAPSHOT',env);self.assertNotIn('DSH_PROFILE',env)
        self.assertNotIn('NODE_OPTIONS',env);self.assertNotIn('NODE_PATH',env)
        self.assertFalse((clean/'cordis.patch.yml').exists())
        self.assertEqual(m.read_json(clean/'profiles/web/package.json')['dsh']['profile']['bundles'],m.OFFICIAL)
        self.assertEqual(m.read_json(self.profile/'package.json'),old)
        self.assertEqual(result['mode'],'safe')

    def test_safe_mode_does_not_take_over_occupied_port(self):
        with patch.object(self.manager,'current_core',return_value={}),patch.object(m,'listening',return_value=True),patch.object(m.subprocess,'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError,'占用'):self.manager.open_safe()
            launch.assert_not_called()

    def test_reused_pid_is_not_signalled(self):
        m.atomic_json(self.manager.state/'safe-process.json',{'pid':123,'identity':'old'})
        with patch.object(m,'process_identity',return_value='new'),patch.object(m.os,'killpg') as kill:
            self.manager.stop_safe();kill.assert_not_called()

    def test_only_one_mutation_can_run(self):
        with self.manager.lock():
            with self.assertRaisesRegex(RuntimeError,'另一个'):
                with m.Maintenance(self.config).lock():pass

    def test_core_switch_is_atomic_and_preserves_original_install(self):
        original=self.root/'old/lib/bin.js';original.parent.mkdir(parents=True);original.write_text('old')
        m.atomic_json(original.parent.parent/'package.json',{'name':m.CORE,'version':'0.1.5-rc.1'})
        target=self.root/'new/lib/bin.js';target.parent.mkdir(parents=True);target.write_text('new')
        m.atomic_json(target.parent.parent/'package.json',{'name':m.CORE,'version':'0.1.7-rc.2'})
        link=self.root/'bin/dsh';link.parent.mkdir();link.symlink_to(original)
        self.manager.config['core_executable']=str(link)
        with patch.object(self.manager,'stop_normal'),patch.object(self.manager,'stop_safe'):
            self.manager.activate(self.manager.core_at(target))
        self.assertEqual(link.resolve(),target)
        self.assertEqual(original.read_text(),'old')
        self.assertEqual(m.read_json(self.manager.state/'selection.json')['originalEntry'],str(original))

    def test_no_symlink_change_if_service_will_not_stop(self):
        target=self.root/'bin/dsh';target.parent.mkdir();target.symlink_to('/old/bin.js')
        self.manager.config['core_executable']=str(target)
        with patch.object(self.manager,'stop_normal',side_effect=RuntimeError('busy')):
            with self.assertRaises(RuntimeError):self.manager.activate({'entry':'/new/bin.js','version':'1.0.0'})
        self.assertEqual(os.readlink(target),'/old/bin.js')

    def test_authentication_tokens_are_redacted(self):
        self.assertEqual(m.redact('http://localhost/?token=secret&x=1'), 'http://localhost/?token=[REDACTED]&x=1')

    def test_versions_include_tags_and_handle_single_release(self):
        with patch.object(self.manager, 'registry_view', return_value={'versions':'1.0.0','dist-tags':{'latest':'1.0.0'}}):
            result = self.manager.versions('example-plugin')
        self.assertEqual(result['versions'], ['1.0.0'])
        self.assertEqual(result['tags'], {'latest':'1.0.0'})
        with patch.object(self.manager, 'registry_view', return_value={'versions':[]}):
            with self.assertRaisesRegex(RuntimeError, '没有可用'): self.manager.versions('example-plugin')

    def test_range_matching_uses_semver_prerelease_and_reports_invalid_unknown(self):
        cases = [
            ('^0.1.5-rc.1', '0.1.7-rc.2', 'dsh', 'match'),
            ('^0.1.7-rc.1', '0.1.5-rc.1', 'dsh', 'mismatch'),
            ('0.1.5-rc.1 || 0.1.2-rc.1', '0.1.7-rc.2', 'dsh', 'mismatch'),
            ('>=0.1.5 <0.2.0', '0.1.7-rc.2', 'dsh', 'match'),
            ('workspace:^', '0.1.5-rc.1', 'dsh', 'match'),
            ('nonsense!', '0.1.5-rc.1', 'dsh', 'invalid'),
            ('>=0.1.5', None, 'dsh', 'unknown'),
            ('>=20', '22.1.0', 'engine', 'match'),
            ('>=24', '22.1.0', 'engine', 'mismatch'),
        ]
        rows = [{'range':r, 'version':v, 'kind':k} for r,v,k,_ in cases]
        self.manager.check_ranges(rows)
        self.assertEqual([r['status'] for r in rows], [status for *_,status in cases])

    def test_version_declarations_available_without_kernel(self):
        meta = {'name':'example-plugin','version':'1.2.3','dsh':{'bundle':{'patch':'x'}},
                'peerDependencies':{m.CORE:'^0.1.5-rc.1','react':'^18'},
                'peerDependenciesMeta':{'react':{'optional':True}},'engines':{'node':'>=20'}}
        with patch.object(self.manager,'registry_view',return_value=meta), \
             patch.object(self.manager,'inventory',return_value={'core':None,'profiles':[]}), \
             patch.object(self.manager,'stop_normal') as stop:
            result = self.manager.dispatch({'action':'version_info','package':'example-plugin','version':'1.2.3'})
        self.assertEqual(result['coreStatus'],'unknown')
        self.assertEqual(result['checks'][0]['name'],m.CORE)
        self.assertEqual(result['checks'][0]['range'],'^0.1.5-rc.1')
        self.assertTrue(result['checks'][1]['optional'])
        self.assertTrue(result['isBundle']); stop.assert_not_called()

    def test_core_candidate_checks_installed_plugins_against_target(self):
        installed = {'name':'example-plugin','version':'1.2.3','enabled':True,'peers':{m.CORE:'0.1.5-rc.1'}}
        state = {'core':{'version':'0.1.5-rc.1'},'profiles':[{'name':'web','error':None,'plugins':[installed]}]}
        with patch.object(self.manager,'registry_view',return_value={'name':m.CORE,'version':'0.1.7-rc.2'}), \
             patch.object(self.manager,'inventory',return_value=state):
            result = self.manager.version_info(m.CORE,'0.1.7-rc.2')
        self.assertEqual(result['targetCore'],'0.1.7-rc.2')
        self.assertEqual(result['installedPlugins'][0]['coreStatus'],'mismatch')
        self.assertEqual(result['installedPlugins'][0]['checks'][0]['version'],'0.1.7-rc.2')

    def test_missing_declarations_are_not_a_compatibility_pass(self):
        with patch.object(self.manager,'registry_view',return_value={'name':'example-plugin','version':'1.2.3'}), \
             patch.object(self.manager,'inventory',return_value={'core':None,'profiles':[]}):
            result = self.manager.version_info('example-plugin','1.2.3')
        self.assertEqual(result['coreStatus'],'undeclared')
        self.assertFalse(result['isBundle'])

    def test_wrong_metadata_version_is_rejected(self):
        with patch.object(self.manager,'registry_view',return_value={'name':'example-plugin','version':'9.9.9'}):
            with self.assertRaisesRegex(RuntimeError, '所选版本不符'):
                self.manager.version_info('example-plugin','1.2.3')

    def test_registry_timeout_has_actionable_error(self):
        with patch.object(self.manager,'tool',return_value='npm'), \
             patch.object(self.manager,'run',side_effect=subprocess.TimeoutExpired('npm',45)):
            with self.assertRaisesRegex(RuntimeError, '查询超时'): self.manager.versions('example-plugin')

if __name__ == '__main__':unittest.main()
