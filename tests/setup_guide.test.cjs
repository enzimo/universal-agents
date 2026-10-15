const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const root = path.resolve(__dirname, '..');
const guide = fs.readFileSync(path.join(root, 'docker-compose-setup.html'),'utf8');
const logic = guide.split('/* GUIDE_LOGIC_START */')[1].split('/* GUIDE_LOGIC_END */')[0];
function page(mode, os, versions={runtimeVersion:'0.5.0',desktopVersion:'0.4.9'}) {
  const values={cpus:'2',memory:'4',disk:'40',container:'universal-agents',data:'~/.universal-agents/data',web:'18090',broker:'18600',image:`ghcr.io/enzimo/universal-agents:${versions.runtimeVersion}`,'external-url':'https://assistant.example.com','public-origin':'http://127.0.0.1','bind-host':'127.0.0.1',instance:'jarvis','host-root':'~/.universal-agents/compose',provider:'openrouter','model-id':'deepseek/deepseek-v4.1-flash','model-endpoint':'https://openrouter.ai/api/v1'};
  const elements={};
  const context=vm.createContext({URL,document:{body:{dataset:{installMode:mode,...versions}},getElementById:id=>elements[id]??=({value:values[id]??'',textContent:'',removeAttribute(name){delete this[name];},addEventListener(event,callback){this[event]=callback;}}),querySelectorAll:()=>[]},navigator:{platform:{macos:'MacIntel',windows:'Win32',linux:'Linux'}[os]}});
  vm.runInContext(logic,context);
  return {elements,run:source=>vm.runInContext(source,context)};
}
test('all operating systems and install modes produce the expected commands',()=>{
  for(const mode of ['runtime','desktop']) for(const os of ['macos','windows','linux']) {
    const p=page(mode,os);assert.equal(p.elements.errors.textContent,'');
    const command=p.elements[mode==='runtime'?'install-command':'installer-command'].textContent;
    assert(command.includes(mode==='runtime'?'bash ua-install.sh':os==='windows'?'Get-FileHash':os==='macos'?'shasum':'sha256sum'));
    if(mode==='runtime') execFileSync('rtk',['proxy','bash','-n'],{input:command});
  }
});
test('runtime and desktop release versions remain independent',()=>{
  const versions={runtimeVersion:'0.6.0',desktopVersion:'0.5.0'};
  assert.match(page('runtime','linux',versions).elements['install-command'].textContent,/v0\.6\.0\/install\.sh/);
  assert.match(page('desktop','linux',versions).elements['installer-command'].textContent,/Desktop-0\.5\.0/);
});
test('invalid settings clear commands, and a valid changed port updates the URL',()=>{
  const p=page('runtime','linux');p.elements.web.value='19090';p.run('render()');
  assert.equal(p.elements['onboarding-link'].href,'http://127.0.0.1:19090/');
  p.elements.broker.value='19090';p.run('render()');assert.match(p.elements.errors.textContent,/different/);assert.equal(p.elements['install-command'].textContent,'');
  p.elements.broker.value='18600';p.elements.data.value='C:\\wrong-context';p.run('render()');assert.match(p.elements.errors.textContent,/Ubuntu path/);
});
test('data directory quoting does not execute shell syntax',()=>{
  const p=page('runtime','linux');const input="/tmp/UA's $(printf injected)";
  const quoted=p.run(`shellQuote(${JSON.stringify(input)})`);
  assert.equal(execFileSync('rtk',['proxy','bash','-c',"printf '%s' "+quoted],{encoding:'utf8'}),input);
});
test('guide is self-contained and private access starts collapsed',()=>{
  assert(!/<script[^>]+\bsrc=/.test(guide));assert(!/<link[^>]+\brel=["']stylesheet/.test(guide));
  assert.match(guide,/<details class="card" id="github-access">/);
  assert(!/<input[^>]+type=["']password/.test(guide));
  assert(!guide.includes('AABBCCDD'));
});
test('hosted service URL controls every service link and public-origin configuration',()=>{
  for (const os of ['macos','windows','linux']) {
    const p=page('hosted',os);p.elements['external-url'].value='https://my-assistant.example:8443/';p.run('render()');
    assert.equal(p.elements.errors.textContent,'');
    assert.equal(p.elements['hosted-chat-link'].href,'https://my-assistant.example:8443/webchat');
    assert.equal(p.elements['hosted-docs-link'].href,'https://my-assistant.example:8443/docs/');
    assert.equal(p.elements['hosted-onboarding-link'].href,'https://my-assistant.example:8443/');
    assert.match(p.elements['server-config'].textContent,/public_origin: "https:\/\/my-assistant.example:8443"/);
    assert.match(p.elements['server-config'].textContent,/broker_public_port: null/);
    assert(!p.elements['install-command']);
  }
});
test('invalid external origins remove previously valid links and generated configuration',()=>{
  for (const value of ['javascript:alert(1)','https://user:secret@example.com','https://example.com/ua','https://example.com/?token=secret','https://example.com/#token','http://0.0.0.0','https://example.com\\evil','https://exam\nple.com']) {
    const p=page('hosted','linux');p.elements['external-url'].value=value;p.run('render()');
    assert.notEqual(p.elements.errors.textContent,'',value);
    assert.equal(p.elements['hosted-chat-link'].href,undefined,value);
    assert.equal(p.elements['server-config'].textContent,'',value);
  }
});
test('LAN installer advertises the server address and separate host ports',()=>{
  const p=page('runtime','linux');p.elements['public-origin'].value='http://192.168.1.50';p.elements['bind-host'].value='192.168.1.50';p.elements.web.value='19090';p.run('render()');
  assert.equal(p.elements.errors.textContent,'');
  assert.equal(p.elements['onboarding-link'].href,'http://192.168.1.50:19090/');
  assert.match(p.elements['install-command'].textContent,/--bind-host '192.168.1.50' --public-origin 'http:\/\/192.168.1.50'/);
  p.elements['public-origin'].value='https://example.com';p.run('render()');assert.match(p.elements.errors.textContent,/Hosted server/);
  p.elements['public-origin'].value='http://example.com:80';p.run('render()');assert.match(p.elements.errors.textContent,/separate fields/);
});
test('Compose settings quote paths, preserve secret files and select the reviewed image',()=>{
  const p=page('runtime','linux');p.elements['host-root'].value="/srv/UA's $(printf injected)";p.elements.instance.value='team-2';p.run('render()');
  const command=p.elements['compose-command'].textContent;
  execFileSync('rtk',['proxy','bash','-n'],{input:command});
  assert.match(command,/export INSTANCE_NAME='team-2'/);
  assert.match(command,/export BUILD_VER='ghcr.io\/enzimo\/universal-agents:0.5.0'/);
  assert.match(command,/if \[ ! -e "\$HOST_ROOT\/secrets\/crawl4ai_api_token" \]/);
  assert.match(command,/openssl rand -hex 32/);
});
test('provider changes clear stale models and separate service and model URLs',()=>{
  const p=page('hosted','linux');p.elements.provider.value='compatible';p.elements.provider.change();
  assert.equal(p.elements['model-id'].value,'');
  p.elements['model-endpoint'].value='http://model.internal:11434/v1';p.elements['model-id'].value='my-model';p.run('render()');
  assert.match(p.elements['model-settings'].textContent,/http:\/\/model.internal:11434\/v1/);
  assert.equal(p.elements['hosted-chat-link'].href,'https://assistant.example.com/webchat');
  p.elements['model-endpoint'].value='https://key:secret@example.com/v1';p.run('render()');
  assert.match(p.elements.errors.textContent,/without credentials/);
});
test('comprehensive guide retains provider signup, server and optional channel instructions',()=>{
  for(const id of ['providers','hosted-setup','server-operator','compose-setup','codex-oauth-section','xai-oauth-section','local-models','search-setup','tg-section','dc-section','signal-section','photon-section']) assert(guide.includes(`id="${id}"`),id);
  for(const url of ['https://openrouter.ai/settings/keys','https://platform.openai.com/api-keys','https://aistudio.google.com/apikey','https://platform.claude.com/','https://dashboard.exa.ai/','https://api-dashboard.search.brave.com/']) assert(guide.includes(url),url);
});
test('guide documents desktop skill refresh and exact diagnostic export',()=>{
  assert.match(guide,/Refresh external skills on start/);
  assert.match(guide,/External skill repositories/);
  assert.match(guide,/Download diagnostics/);
});
