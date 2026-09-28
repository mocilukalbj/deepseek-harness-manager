"""Destructive operations are exercised only in temporary fixture installations."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import maintenance as m


class CoreUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dsh-uninstall-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.link = self.root/'bin/dsh'
        self.manager = m.Maintenance({'home':str(self.root),'port':13089,
            'management_home':str(self.root/'manager'),'core_executable':str(self.link)})
        self.current = self.make_managed('0.1.7-rc.2','current')
        self.old = self.make_managed('0.1.5-rc.1','old')
        self.link.parent.mkdir();self.link.symlink_to(self.current['entry'])
        self.selection = self.manager.state/'selection.json'
        m.atomic_json(self.selection,{'activationPath':str(self.link),'originalEntry':self.old['entry'],'version':self.current['version']})
        self.data = self.root/'.dsh/storages/session.json'
        m.atomic_json(self.data,{'message':'keep my session'})
        self.profile = self.root/'.dsh/profiles/web/package.json'
        m.atomic_json(self.profile,{'dependencies':{'keep-plugin':'1.0.0'},'dsh':{'profile':{'bundles':['keep-plugin']}}})
        for name in ('stop_normal','stop_safe'):
            p=patch.object(self.manager,name);setattr(self,name,p.start());self.addCleanup(p.stop)
        p=patch.object(m,'listening',return_value=False);p.start();self.addCleanup(p.stop)

    def make_managed(self,version,suffix):
        folder=self.manager.state/'kernels'/f'{version}-{suffix}'
        entry=folder/'node_modules/@deepseek-ai/dsh/lib/bin.js'
        entry.parent.mkdir(parents=True)
        entry.write_text(f'console.log({json.dumps(version)});')
        m.atomic_json(entry.parent.parent/'package.json',{'name':m.CORE,'version':version,'bin':{'dsh':'lib/bin.js'}})
        m.atomic_json(folder/'package.json',{'name':'dsh-shell-runtime','private':True,'dependencies':{m.CORE:version}})
        m.atomic_json(folder/'ready.json',{'entry':str(entry),'version':version})
        return self.manager.core_at(entry)

    def make_global(self):
        prefix=self.root/'npm'
        entry=prefix/'lib/node_modules/@deepseek-ai/dsh/lib/bin.js'
        entry.parent.mkdir(parents=True);entry.write_text('console.log("old");')
        m.atomic_json(entry.parent.parent/'package.json',{'name':m.CORE,'version':'0.1.5-rc.1',
            'bin':{'dsh':'lib/bin.js'},'scripts':{'uninstall':'touch '+str(self.root/'unexpected-script')}})
        self.link=prefix/'bin/dsh';self.link.parent.mkdir();self.link.symlink_to(self.current['entry'])
        self.manager.config['core_executable']=str(self.link)
        m.atomic_json(self.selection,{'activationPath':str(self.link),'originalEntry':str(entry),'version':self.current['version']})
        return self.manager.core_at(entry)

    def uninstall(self,core):
        return self.manager.dispatch({'action':'uninstall_core','version':core['version'],'entry':core['entry']})

    def test_managed_removal_preserves_active_runtime_user_data_and_plugins(self):
        before=self.profile.read_bytes()
        result=self.uninstall(self.old)
        self.assertFalse(Path(self.old['root']).parents[2].exists())
        self.assertTrue(Path(self.current['entry']).exists())
        self.assertEqual(self.link.resolve(),Path(self.current['entry']))
        self.assertEqual(self.profile.read_bytes(),before)
        self.assertEqual(m.read_json(self.data),{'message':'keep my session'})
        self.assertNotIn('originalEntry',m.read_json(self.selection))
        self.assertIn('已卸载',result['message'])
        self.stop_normal.assert_called_once();self.stop_safe.assert_called_once()
        self.assertEqual(len(self.manager.inventory()['cores']),1)

    def test_current_runtime_is_protected_before_stopping(self):
        with self.assertRaisesRegex(RuntimeError,'当前使用'):self.uninstall(self.current)
        self.stop_normal.assert_not_called();self.stop_safe.assert_not_called()
        self.assertTrue(Path(self.current['entry']).exists())

    def test_same_version_installations_are_targeted_by_entry(self):
        duplicate=self.make_managed(self.current['version'],'duplicate')
        self.uninstall(duplicate)
        self.assertTrue(Path(self.current['entry']).exists())
        self.assertFalse(Path(duplicate['entry']).exists())
        self.assertTrue(Path(self.old['entry']).exists())

    def test_switch_same_version_targets_the_selected_installation(self):
        duplicate=self.make_managed(self.current['version'],'duplicate')
        self.manager.dispatch({'action':'switch_core','version':duplicate['version'],'entry':duplicate['entry']})
        self.assertEqual(self.link.resolve(),Path(duplicate['entry']))

    def test_unregistered_or_stale_targets_are_rejected(self):
        for entry,version in [('/tmp/unknown-core/lib/bin.js','0.1.5-rc.1'),(self.old['entry'],'9.9.9'),('../kernel','0.1.5-rc.1')]:
            with self.assertRaises(ValueError):self.manager.dispatch({'action':'uninstall_core','entry':entry,'version':version})
        self.assertTrue(Path(self.old['entry']).exists());self.stop_normal.assert_not_called()

    def test_failed_stop_preserves_installation_and_selection(self):
        before=self.selection.read_bytes();self.stop_normal.side_effect=RuntimeError('cannot stop')
        with self.assertRaisesRegex(RuntimeError,'cannot stop'):self.uninstall(self.old)
        self.assertTrue(Path(self.old['entry']).exists());self.assertEqual(self.selection.read_bytes(),before)

    def test_newly_activated_target_is_rechecked_before_deletion(self):
        def activate_old():self.link.unlink();self.link.symlink_to(self.old['entry'])
        self.stop_normal.side_effect=activate_old
        with self.assertRaisesRegex(RuntimeError,'当前使用'):self.uninstall(self.old)
        self.assertTrue(Path(self.old['entry']).exists())

    def test_unowned_safe_port_blocks_deletion(self):
        with patch.object(m,'listening',side_effect=lambda port:port==self.manager.safe_port):
            with self.assertRaisesRegex(RuntimeError,'端口仍被占用'):self.uninstall(self.old)
        self.assertTrue(Path(self.old['entry']).exists())

    def test_symlinked_installation_cannot_delete_outside_managed_directory(self):
        folder=Path(self.old['root']).parents[2];outside=self.root/'outside'
        folder.rename(outside);folder.symlink_to(outside,target_is_directory=True)
        with self.assertRaises((ValueError,RuntimeError)):self.uninstall(self.old)
        self.assertTrue((outside/'ready.json').exists())

    def test_invalid_installation_record_is_not_removable(self):
        folder=Path(self.old['root']).parents[2]
        m.atomic_json(folder/'package.json',{'name':'other-project','dependencies':{m.CORE:self.old['version']}})
        rows=self.manager.inventory()['cores']
        self.assertFalse(next(c for c in rows if c['entry']==self.old['entry'])['canUninstall'])
        with self.assertRaisesRegex(ValueError,'安装记录'):self.uninstall(self.old)
        self.stop_normal.assert_not_called()

    def test_data_inside_installation_is_protected(self):
        self.manager.dsh_home=Path(self.old['root'])/'user-data'
        m.atomic_json(self.manager.dsh_home/'keep.json',{'keep':True})
        with self.assertRaisesRegex(ValueError,'用户数据'):self.uninstall(self.old)
        self.assertTrue((self.manager.dsh_home/'keep.json').exists())

    def test_delete_failure_keeps_registration(self):
        before=self.selection.read_bytes()
        with patch.object(m.shutil,'rmtree',side_effect=OSError('read only')):
            with self.assertRaises(OSError):self.uninstall(self.old)
        self.assertEqual(self.selection.read_bytes(),before)

    def test_missing_active_kernel_still_allows_old_runtime_cleanup(self):
        self.link.unlink()
        self.uninstall(self.old)
        self.assertFalse(Path(self.old['entry']).exists())

    def test_npm_global_uninstall_restores_active_link_and_keeps_other_packages(self):
        old=self.make_global();prefix=Path(old['root']).parents[3]
        other=prefix/'lib/node_modules/other-package/package.json'
        m.atomic_json(other,{'name':'other-package','version':'1.0.0'})
        self.manager.env['npm_config_cache']=str(self.root/'npm-cache')
        result=self.uninstall(old)  # Real npm, offline, temporary prefix only.
        self.assertFalse(Path(old['root']).exists())
        self.assertTrue(other.exists());self.assertTrue(self.data.exists())
        self.assertEqual(self.link.resolve(),Path(self.current['entry']))
        self.assertNotIn('originalEntry',m.read_json(self.selection))
        self.assertFalse((self.root/'unexpected-script').exists())
        output=subprocess.check_output([self.manager.tool('node'),str(self.link)],text=True)
        self.assertEqual(output.strip(),self.current['version'])
        self.assertIn('已卸载',result['message'])

    def test_npm_failure_restores_link_and_keeps_original_record(self):
        old=self.make_global();before=self.selection.read_bytes()
        def failing_npm(*args,**kwargs):self.link.unlink();raise RuntimeError('npm failed')
        with patch.object(self.manager,'run',side_effect=failing_npm):
            with self.assertRaisesRegex(RuntimeError,'npm failed'):self.uninstall(old)
        self.assertEqual(self.link.resolve(),Path(self.current['entry']))
        self.assertEqual(self.selection.read_bytes(),before)

    def test_unexpected_npm_bin_declaration_is_rejected(self):
        old=self.make_global();p=Path(old['root'])/'package.json';data=m.read_json(p)
        data['bin']['other-tool']='lib/bin.js';m.atomic_json(p,data)
        with self.assertRaisesRegex(ValueError,'入口声明异常'):self.uninstall(old)
        self.stop_normal.assert_not_called()

    def test_changed_npm_entry_is_not_overwritten(self):
        old=self.make_global()
        def change_link():self.link.unlink();self.link.symlink_to('/some/other/kernel')
        self.stop_normal.side_effect=change_link
        with patch.object(self.manager,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'npm 入口发生变化'):self.uninstall(old)
            run.assert_not_called()
        self.assertEqual(os.readlink(self.link),'/some/other/kernel')


if __name__=='__main__':unittest.main()
