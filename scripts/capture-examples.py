"""Render documentation examples with the real UI and fixed, non-personal data.

Requires a graphical Linux session, Python GI, GTK 3 and WebKit2 4.1.
All IPC is replaced with read-only fixtures; no kernel, plugin, or network calls.
"""
import argparse
import json
from pathlib import Path

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('WebKit2', '4.1')
from gi.repository import Gtk, WebKit2, GLib

SCENES = [
    ('01-launch', 1280, 920),
    ('02-kernel-versions', 1280, 1260),
    ('03-kernel-compatibility', 1280, 800),
    ('04-plugins', 1280, 940),
    ('05-plugin-incompatible', 1280, 1140),
    ('06-plugin-compatible', 1280, 1140),
    ('07-no-kernel', 1280, 920),
    ('08-no-kernel-declarations', 1280, 1140),
    ('09-query-error', 1280, 1100),
    ('10-uninstall-core', 1280, 980),
]

BRIDGE = r'''
window.exampleCalls = [];
window.exampleStatus = JSON.parse(JSON.stringify(window.exampleData.status));
if (window.exampleScene === '07-no-kernel' || window.exampleScene === '08-no-kernel-declarations') {
  Object.assign(window.exampleStatus, {core:null, cores:[], coreError:'未检测到可用内核，可在版本管理中安装。', normalRunning:false});
}
if (window.exampleScene === '10-uninstall-core') {
  const old = structuredClone(window.exampleStatus.core);
  const folder='/home/demo/.local/share/dsh-tauri/kernels/0.1.7-rc.2-example';
  window.exampleStatus.core={version:'0.1.7-rc.2',root:folder+'/node_modules/@deepseek-ai/dsh',entry:folder+'/node_modules/@deepseek-ai/dsh/lib/bin.js'};
  window.exampleStatus.cores=[{...window.exampleStatus.core,current:true,canUninstall:false,uninstallKind:'managed',uninstallReason:'请先切换到其他内核，再卸载当前版本。'},
    {...old,current:false,canUninstall:true,uninstallKind:'npm-global',uninstallPath:old.root}];
}
window.__TAURI__ = {core:{invoke:async(command,args)=>{
  if(command === 'startup_mode') return 'manage';
  const r = args.request;
  window.exampleCalls.push(r);
  if(r.action === 'status') return structuredClone(window.exampleStatus);
  if(r.action === 'versions') {
    if(window.exampleScene === '09-query-error') throw new Error('版本查询超时，请检查网络和 npm registry 后重试。');
    if(!window.exampleData.versions[r.package]) throw new Error('No example versions for '+r.package);
    return structuredClone(window.exampleData.versions[r.package]);
  }
  if(r.action === 'version_info') {
    const key = r.package+'@'+r.version;
    if(!window.exampleData.metadata[key]) throw new Error('No example metadata for '+key);
    const info = structuredClone(window.exampleData.metadata[key]);
    if(!window.exampleStatus.core && r.package !== '@deepseek-ai/dsh') {
      info.currentCore = null;info.targetCore = null;info.coreStatus = 'unknown';
      info.checks.filter(row=>row.kind==='dsh').forEach(row=>{row.version=null;row.status='unknown';});
    }
    return info;
  }
  throw new Error('Documentation capture cannot mutate installations: '+r.action);
}}};
'''

PREPARE = r'''
(async()=>{
  if (!state) await refresh();
  const scene = window.exampleScene;
  const focus = el => window.scrollTo(0, Math.max(0, el.getBoundingClientRect().top + window.scrollY - 24));
  if (scene === '01-launch' || scene === '07-no-kernel') {
    show('launch');
    notice(scene === '07-no-kernel' ? '示例：尚未安装内核，版本管理仍然可用。' : '示例：日常环境已就绪，可选择普通模式或安全模式。');
    window.scrollTo(0,0);
  } else if (scene === '10-uninstall-core') {
    show('kernel');notice('示例：已切换到新内核，可确认卸载旧的 npm 全局安装。');
    $('core-list').querySelector('.version-actions .danger').click();
    window.scrollTo(0,0);
  } else if (scene.startsWith('02-') || scene.startsWith('03-') || scene.startsWith('09-')) {
    show('kernel');$('core-version').value='0.1.7-rc.2';await fetchVersions('core');
    if(scene.startsWith('03-')) {
      $('core-compat').querySelector('details').open=true;
      focus($('core-compat'));
    } else if(scene.startsWith('09-')) {
      window.scrollTo(0,0);
    } else window.scrollTo(0,0);
  } else if (scene.startsWith('04-')) {
    show('plugins');notice('示例：查看已装插件、启用状态和本地源码插件。');window.scrollTo(0,0);
  } else {
    show('plugins');$('plugin-name').value='dsh-better-sidebar';
    $('plugin-version').value=scene.startsWith('06-')?'0.19.1':'0.22.1';
    await fetchVersions('plugin');focus($('plugin-form-title').closest('.panel'));
  }
  await document.fonts.ready;
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  if(window.exampleCalls.some(r=>!['status','versions','version_info'].includes(r.action))) throw new Error('Unexpected mutation');
  if(document.documentElement.scrollWidth>window.innerWidth) throw new Error('Page overflows horizontally');
  if(['02-kernel-versions','03-kernel-compatibility'].includes(scene) && !$('core-compat').textContent.includes('不匹配')) throw new Error('Missing core mismatch example');
  if(scene==='05-plugin-incompatible' && !$('plugin-compat').textContent.includes('不匹配')) throw new Error('Missing mismatch');
  if(scene==='06-plugin-compatible' && $('plugin-compat').textContent.includes('不匹配')) throw new Error('Wrong compatible example');
  if(scene==='08-no-kernel-declarations' && !$('plugin-compat').textContent.includes('未核验')) throw new Error('Missing unknown state');
  if(scene==='10-uninstall-core' && $('core-list').querySelector('.uninstall-confirmation').hidden) throw new Error('Missing uninstall confirmation');
  if(scene==='09-query-error'  && !$('core-version-count').textContent.includes('失败')) throw new Error('Missing failure state');
  window.exampleReady=true;
})().catch(e=>window.exampleError=String(e));
'''


def capture(root, data, output, scene, width, height):
    content = WebKit2.UserContentManager()
    script = 'window.exampleData='+json.dumps(data)+';window.exampleScene='+json.dumps(scene)+';'+BRIDGE
    content.add_script(WebKit2.UserScript.new(script, WebKit2.UserContentInjectedFrames.TOP_FRAME,
                                              WebKit2.UserScriptInjectionTime.START, None, None))
    view = WebKit2.WebView.new_with_user_content_manager(content)
    window = Gtk.OffscreenWindow()
    window.set_default_size(width,height)
    window.add(view);window.show_all()
    errors = []; completed = False

    def finish(v, result):
        nonlocal completed
        try:
            surface = v.get_snapshot_finish(result)
            surface.write_to_png(str(output/(scene+'.png')))
            print(f'{scene}: {surface.get_width()} × {surface.get_height()}',flush=True)
            completed = True
        except Exception as error: errors.append(str(error))
        Gtk.main_quit()

    def inspected(v, result):
        try:
            state = json.loads(v.run_javascript_finish(result).get_js_value().to_string())
            if state.get('error'):
                errors.append(state['error']);Gtk.main_quit()
            elif state.get('ready'):
                v.get_snapshot(WebKit2.SnapshotRegion.VISIBLE,WebKit2.SnapshotOptions.NONE,None,finish)
            else: GLib.timeout_add(75,poll)
        except Exception as error:
            errors.append(str(error));Gtk.main_quit()

    def poll():
        view.run_javascript('JSON.stringify({ready:window.exampleReady,error:window.exampleError})',None,inspected)
        return False

    def prepare():
        view.run_javascript(PREPARE,None,None)
        GLib.timeout_add(150,poll)
        return False

    def loaded(v,event):
        if event == WebKit2.LoadEvent.FINISHED: GLib.timeout_add(250,prepare)

    def timed_out():
        errors.append('Render timed out');Gtk.main_quit();return False

    view.connect('load-changed',loaded)
    timer=GLib.timeout_add_seconds(20,timed_out)
    view.load_uri((root/'ui/index.html').as_uri())
    Gtk.main();GLib.source_remove(timer);window.destroy()
    if errors or not completed: raise RuntimeError(scene+': '+repr(errors))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--fixtures',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--only',choices=[s[0] for s in SCENES])
    args=parser.parse_args()
    root=args.root.resolve()
    fixtures=args.fixtures or root/'docs/screenshots/fixtures.json'
    output=args.output or root/'docs/screenshots'
    output=output.resolve();output.mkdir(parents=True,exist_ok=True)
    data=json.loads(fixtures.read_text())
    for scene,width,height in SCENES:
        if not args.only or scene==args.only: capture(root,data,output,scene,width,height)


if __name__=='__main__':main()
