import json,sys
from pathlib import Path
import gi
gi.require_version('Gtk','3.0');gi.require_version('WebKit2','4.1')
from gi.repository import Gtk,WebKit2,GLib
root=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parents[1]
screenshot=Path(sys.argv[2]).resolve() if len(sys.argv)>2 else None
fixture={'core':None,'coreError':'未安装内核','cores':[],'normalRunning':False,'safeRunning':False,'profiles':[{'name':'web','error':None,'plugins':[{'name':'dsh-better-sidebar','version':'0.19.1','spec':'^0.19.1','enabled':True,'peers':{'@deepseek-ai/dsh-llm':'^0.1.5-rc.1'},'local':False},{'name':'dsh-kernel-launcher','version':'0.1.0','spec':'link:/local/plugin','enabled':True,'peers':{},'local':True}]}],'logTail':''}
inject='window.testRequests=[];window.testFixture='+json.dumps(fixture)+';'+r'''
window.__TAURI__={core:{invoke:async(cmd,args)=>{
 if(cmd==='startup_mode')return 'manage';
 const r=args.request;window.testRequests.push(r);
 if(r.action==='status')return JSON.parse(JSON.stringify(window.testFixture));
 if(r.action==='uninstall_core'){
  if(window.failUninstall)throw new Error('模拟卸载失败');
  const selected=window.testFixture.cores.find(c=>c.entry===r.entry&&c.version===r.version);
  if(!selected?.canUninstall||selected.current)throw new Error('protected runtime');
  window.testFixture.cores=window.testFixture.cores.filter(c=>c.entry!==r.entry);
  return {message:'已卸载内核 '+r.version};
 }
 if(r.package==='fail-plugin')throw new Error('registry offline');
 if(r.action==='versions'){
  if(r.package==='slow-plugin')await new Promise(resolve=>setTimeout(resolve,300));
  if(r.package==='empty-plugin')return {versions:[]};
  return r.package==='@deepseek-ai/dsh'?{versions:['0.1.7-rc.2','0.1.5-rc.1'],tags:{latest:'0.1.7-rc.2'}}:{versions:['0.22.1','0.19.1'],tags:{latest:'0.22.1'}};
 }
 if(r.action==='version_info'){
  await new Promise(resolve=>setTimeout(resolve,60));
  const core=window.testFixture.core?.version;
  const result=!core?'unknown':r.version==='0.22.1'?'mismatch':'match';
  return {package:r.package,version:r.version,currentCore:core,targetCore:r.version,nodeVersion:'22.1.0',isBundle:true,coreStatus:result,deprecated:null,
   checks:[{name:'@deepseek-ai/dsh-llm',range:r.version==='0.22.1'?'^0.1.7-rc.1':'^0.1.5-rc.1',version:core,kind:'dsh',status:result},{name:'react',range:'^18',kind:'other',status:'unknown'}],
   engines:[{name:'node',range:'>=20',version:'22.1.0',status:'match'}],
   installedPlugins:[{profile:'web',name:'agent-teams',version:'0.1.20',enabled:true,coreStatus:r.version==='0.1.7-rc.2'?'mismatch':'match',checks:[{name:'@deepseek-ai/dsh',range:'0.1.5-rc.1',version:r.version,status:r.version==='0.1.7-rc.2'?'mismatch':'match'}]}],profileErrors:[]};
 }
 throw new Error('unexpected mutation '+r.action);
}}};
'''
manager=WebKit2.UserContentManager();manager.add_script(WebKit2.UserScript.new(inject,WebKit2.UserContentInjectedFrames.TOP_FRAME,WebKit2.UserScriptInjectionTime.START,None,None))
view=WebKit2.WebView.new_with_user_content_manager(manager)
window=Gtk.OffscreenWindow();window.set_default_size(1120,840);window.add(view);window.show_all()
errors=[];done=False

def snapshot_finished(v,result):
 global done
 try:
  surface=v.get_snapshot_finish(result)
  if screenshot: surface.write_to_png(str(screenshot))
  print('PASS: WebKit version manager interactions verified',flush=True)
 except Exception as e:errors.append(str(e))
 done=True;Gtk.main_quit()

def tested(v,result):
 try:
  value=json.loads(v.run_javascript_finish(result).get_js_value().to_string())
  if value is None:
   GLib.timeout_add(100,poll);return
  print(json.dumps(value,ensure_ascii=False),flush=True)
  errors.extend(k for k,ok in value.items() if ok is not True)
 except Exception as e:errors.append(str(e))
 view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE,WebKit2.SnapshotOptions.NONE,None,snapshot_finished)

def poll():
 view.run_javascript('JSON.stringify(window.testResult||null)',None,tested)
 return False

def inspect():
 view.run_javascript(r'''
(async()=>{
 const d=id=>document.getElementById(id), wait=ms=>new Promise(r=>setTimeout(r,ms));
 const until=async(fn)=>{for(let i=0;i<100;i++){if(fn())return;await wait(20)}throw new Error('UI timeout')};
 const result={};
 result.noKernelManager=d('current-version').textContent.includes('未安装');
 result.versionManagementEnabled=!d('core-install').disabled;
 result.launchesDisabled=d('open-normal').disabled&&d('open-safe').disabled;
 result.pluginsVisibleWithoutKernel=document.querySelectorAll('.plugin').length===2;
 result.noBackendAutoStart=window.testRequests.every(r=>r.action==='status');
 document.querySelector('[data-page="kernel"]').click();d('core-fetch').click();
 await until(()=>d('core-compat').textContent.includes('运行环境声明'));
 result.visibleVersionList=d('core-versions').tagName==='SELECT'&&d('core-versions').size===6&&d('core-versions').options.length===2&&!d('core-versions').disabled&&d('core-versions').getBoundingClientRect().height>100;
 result.tagsVisible=d('core-versions').textContent.includes('latest')&&d('core-versions').textContent.includes('预发布');
 result.coreCandidateMismatch=d('core-compat').textContent.includes('agent-teams')&&d('core-compat').textContent.includes('不匹配');
 document.querySelector('[data-page="plugins"]').click();document.querySelector('.plugin-controls button').click();
 await until(()=>d('plugin-compat').textContent.includes('运行环境声明'));
 result.installedVersionSelected=d('plugin-version').value==='0.19.1';
 result.declarationsWithoutKernel=d('plugin-compat').textContent.includes('@deepseek-ai/dsh-llm')&&d('plugin-compat').textContent.includes('^0.1.5-rc.1')&&d('plugin-compat').textContent.includes('未核验');
 window.testFixture.core={version:'0.1.5-rc.1'};window.testFixture.coreError=null;d('refresh').click();await wait(50);
 d('plugin-versions').value='0.22.1';d('plugin-versions').dispatchEvent(new Event('change'));
 await until(()=>d('plugin-compat').textContent.includes('运行环境声明'));
 result.candidateMismatch=d('plugin-version').value==='0.22.1'&&d('plugin-compat').textContent.includes('不匹配')&&d('plugin-compat').textContent.includes('^0.1.7-rc.1');
 d('plugin-versions').value='0.19.1';d('plugin-versions').dispatchEvent(new Event('change'));
 await until(()=>d('plugin-compat').textContent.includes('运行环境声明'));
 result.selectionRefreshesDeclarations=d('plugin-compat').textContent.includes('^0.1.5-rc.1')&&!d('plugin-compat').textContent.includes('不匹配');
 d('plugin-details').click();d('plugin-version').value='0.99.0';d('plugin-version').dispatchEvent(new Event('input'));await wait(120);
 result.staleDetailsDiscarded=d('plugin-compat').textContent.includes('版本已变化')&&!d('plugin-compat').textContent.includes('^0.1.5');
 for(const name of ['slow-plugin','fail-plugin','empty-plugin']){
  d('plugin-name').value=name;d('plugin-name').dispatchEvent(new Event('input'));d('plugin-fetch').click();
  if(name==='slow-plugin'){
   d('plugin-name').value='renamed-plugin';d('plugin-name').dispatchEvent(new Event('input'));await wait(400);
   result.staleListDiscarded=d('plugin-version-count').textContent.includes('点击')&&d('plugin-versions').disabled;
  }else{
   await until(()=>d('plugin-version-count').textContent.includes('失败'));
   result[name==='fail-plugin'?'fetchErrorVisible':'emptyResultVisible']=!d('plugin-fetch').disabled&&d('plugin-compat').textContent.includes(name==='fail-plugin'?'offline':'没有可用');
  }
 }
 d('plugin-name').value='dsh-better-sidebar';d('plugin-name').dispatchEvent(new Event('input'));d('plugin-fetch').click();
 await until(()=>d('plugin-compat').textContent.includes('运行环境声明'));
 d('plugin-form-title').scrollIntoView();
 result.noHorizontalOverflow=document.documentElement.scrollWidth<=window.innerWidth;
 result.readOnlyVersionQueries=window.testRequests.every(r=>['status','versions','version_info'].includes(r.action));
 const active='/managed/current/lib/bin.js',old='/managed/old/lib/bin.js';
 window.testFixture.cores=[
  {version:'0.1.5-rc.1',entry:active,current:true,canUninstall:false,uninstallReason:'请先切换到其他内核，再卸载当前版本。'},
  {version:'0.1.5-rc.1',entry:old,current:false,canUninstall:true,uninstallKind:'managed',uninstallPath:'/managed/old'},
  {version:'0.1.2-rc.1',entry:'/external/bin.js',current:false,canUninstall:false,uninstallReason:'外部安装：请使用原安装方式卸载。'}];
 d('refresh').click();await wait(50);document.querySelector('[data-page="kernel"]').click();
 const row=entry=>Array.from(document.querySelectorAll('.version-item')).find(el=>el.textContent.includes(entry));
 result.currentRuntimeProtected=!row(active).querySelector('.danger');
 result.externalRuntimeExplained=row('/external/bin.js').textContent.includes('外部安装')&&!row('/external/bin.js').querySelector('.danger');
 const countBefore=window.testRequests.filter(r=>r.action==='uninstall_core').length;
 row(old).querySelector('.version-actions .danger').click();
 result.uninstallConfirmationShowsTarget=!row(old).querySelector('.uninstall-confirmation').hidden&&row(old).querySelector('.uninstall-confirmation').textContent.includes('/managed/old');
 Array.from(row(old).querySelectorAll('button')).find(b=>b.textContent==='取消').click();
 result.uninstallCancelIsReadOnly=row(old).querySelector('.uninstall-confirmation').hidden&&window.testRequests.filter(r=>r.action==='uninstall_core').length===countBefore;
 window.failUninstall=true;
 row(old).querySelector('.version-actions .danger').click();
 row(old).querySelector('.uninstall-confirmation .danger').click();
 await until(()=>d('notice').textContent.includes('模拟卸载失败')&&!row(old).querySelector('.version-actions .danger').disabled);
 result.failedUninstallKeepsVersion=!!row(old);
 window.failUninstall=false;
 row(old).querySelector('.version-actions .danger').click();row(old).querySelector('.uninstall-confirmation .danger').click();
 await until(()=>!row(old));
 const last=window.testRequests.filter(r=>r.action==='uninstall_core').at(-1);
 result.uninstallTargetsExactInstallation=last.entry===old&&last.version==='0.1.5-rc.1'&&!!row(active);
 document.querySelector('[data-page="plugins"]').click();d('plugin-form-title').scrollIntoView();
 window.testResult=result;
})().catch(e=>window.testResult={error:String(e)});
''',None,None)
 GLib.timeout_add(100,poll);return False

def loaded(v,event):
 if event==WebKit2.LoadEvent.FINISHED:GLib.timeout_add(500,inspect)
view.connect('load-changed',loaded);view.load_uri((root/'ui/index.html').as_uri())
GLib.timeout_add_seconds(25,lambda:(Gtk.main_quit(),False)[1]);Gtk.main();window.destroy()
if errors or not done:raise SystemExit('UI failed: '+repr(errors))
