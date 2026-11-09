// Executes the actual settings page script, with read-only HTTP and DOM doubles.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/settings.html','utf8');
const source=html.match(/<script>\s*([\s\S]*?)<\/script>/)?.[1]||'';
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function setup(health){
  const nodes=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],{textContent:'',hidden:false,disabled:false,events:{},addEventListener(name,fn){this.events[name]=fn;}}]));
  const control={health,fail:false,failedPaths:new Set(),routes:{
    '/observe/channel-runtime':{ok:true,mode:'send',core_enabled:true,started_at:'2026-11-09T01:00:00Z',core_timeout_ms:180000,disk_matches:true,config_error:null,gate_words:{chitchatSampleMod:10},policy:JSON.parse(fs.readFileSync(__dirname+'/../../channels/onebot/data/policy.json','utf8'))},
    '/observe/channel':{ok:true,bots:[{self_id:'12345',running:true,business_connected:true}],checked_at:'2026-11-08T10:00:00+09:00'},
    '/eye/status':{ok:true,config:{enabled:true},credential_ready:true},
    '/ear/status':{ok:true,config:{enabled:true},model_ready:true},
    '/voice/status':{ok:true,config:{enabled:false},credential_ready:true}}},requests=[];
  vm.runInNewContext(source,{document:{getElementById:id=>nodes.get(id)},
    panelFetch:async(path,init)=>{requests.push({path,method:init.method});const failed=control.fail||control.failedPaths.has(path);return {status:failed?503:200,json:async()=>failed?{message:'连接失败'}:path==='/healthz'?control.health:control.routes[path]};}});
  return {get:id=>nodes.get(id),control,requests};
}
const health={ok:true,source_changed:false,dirty:true,stale:false,git_head:'abc',git_head_now:'abc',pid:123,started_at:'2026-11-07T10:00:00Z',source_digest:'aaaa',source_digest_now:'aaaa'};
test('shows synchronized runtime separately from an uncommitted workspace',async()=>{
  const app=setup(health);await settle();
  assert.ok(app.get('runtime-status'),'settings needs visible runtime status');
  assert.match(app.get('runtime-status').textContent,/源码与启动时一致/);
  assert.match(app.get('runtime-status').textContent,/未提交/);
  assert.equal(app.get('runtime-details').hidden,false);
  assert.ok(app.requests.every(r=>['/healthz','/observe/channel','/observe/channel-runtime','/eye/status','/ear/status','/voice/status'].includes(r.path)&&r.method==='GET'));
});
test('changed Python source is visible without a new commit',async()=>{
  const app=setup({...health,source_changed:true,source_digest_now:'bbbb'});await settle();
  assert.match(app.get('runtime-status')?.textContent||'',/重新加载/);
  assert.match(app.get('runtime-info').textContent,/aaaa[\s\S]*bbbb/);
});
test('missing old-server field does not claim code is synchronized',async()=>{
  const app=setup({...health,source_changed:undefined});await settle();
  assert.match(app.get('runtime-status')?.textContent||'',/尚未提供/);
});
test('failed refresh hides stale details and can recover without a write request',async()=>{
  const app=setup(health);await settle();
  assert.ok(app.get('runtime-refresh'),'missing refresh control');
  app.control.fail=true;app.get('runtime-refresh').events.click();await settle();
  assert.match(app.get('runtime-status').textContent,/503/);
  assert.equal(app.get('runtime-details').hidden,true);
  assert.equal(app.get('runtime-refresh').disabled,false);
  app.control.fail=false;app.get('runtime-refresh').events.click();await settle();
  assert.equal(app.get('runtime-details').hidden,false);
  assert.match(app.get('runtime-status').textContent,/源码与启动时一致/);
  assert.ok(app.requests.every(r=>r.method==='GET'));
});

test('shows connection and configured-but-disabled voice separately',async()=>{
  const app=setup(health);await settle();
  assert.match(app.get('channel-status')?.textContent||'',/12345.*协议已连接.*业务已连接/);
  assert.match(app.get('eye-status')?.textContent||'',/已开启.*账号已配置/);
  assert.match(app.get('ear-status')?.textContent||'',/已开启.*本地模型已就绪/);
  assert.match(app.get('voice-status')?.textContent||'',/已关闭.*账号已配置/);
  assert.equal(app.requests.length,6);
});

test('one failure does not hide healthy components and refresh recovers',async()=>{
  const app=setup(health);await settle();
  app.control.failedPaths.add('/observe/channel');
  app.control.routes['/eye/status'].credential_ready=false;
  app.get('runtime-refresh').events.click();await settle();
  assert.match(app.get('channel-status')?.textContent||'',/暂时读不到/);
  assert.equal(app.get('channel-checked').textContent,'');
  assert.match(app.get('eye-status').textContent,/账号未配置/);
  assert.match(app.get('ear-status').textContent,/已开启/);
  assert.equal(app.get('runtime-refresh').disabled,false);
  app.control.failedPaths.clear();app.control.routes['/observe/channel'].bots=[];
  app.get('runtime-refresh').events.click();await settle();
  assert.match(app.get('channel-status').textContent,/尚无 QQ 协议连接/);
  app.control.routes['/observe/channel'].bots=[{self_id:'12345',business_connected:false,running:false}];
  app.get('runtime-refresh').events.click();await settle();
  assert.match(app.get('channel-status').textContent,/业务未连接.*转接未运行/);
  assert.ok(app.requests.every(r=>r.method==='GET'));
});

test('shows loaded merge windows and sampling without promising a reply percentage',async()=>{
 const app=setup(health);await settle();
 assert.match(app.get('channel-rules').textContent,/私聊.*3.5 秒/);
 assert.match(app.get('channel-rules').textContent,/群聊.*2 秒.*5 秒/);
 assert.match(app.get('channel-rules').textContent,/最多等 8 秒.*6 条/);
 assert.match(app.get('channel-rules').textContent,/每段最多 512 字、8 句；每轮最多 3 段/);
 assert.match(app.get('channel-rules').textContent,/全部消息段编号.*每 10 段.*普通闲聊.*参与机会/);
 assert.match(app.get('channel-rules').textContent,/不等于最终回复比例/);
 assert.match(app.get('channel-rules-state').textContent,/与磁盘配置一致/);
 app.control.routes['/observe/channel-runtime'].disk_matches=false;
 app.get('runtime-refresh').events.click();await settle();
 assert.match(app.get('channel-rules-state').textContent,/重新启动.*生效/);
 assert.match(app.get('channel-rules').textContent,/每 10 段/);
});
test('runtime failure clears stale settings and no-core mode does not claim merge rules',async()=>{
 const app=setup(health);await settle();
 app.control.failedPaths.add('/observe/channel-runtime');
 app.get('runtime-refresh').events.click();await settle();
 assert.match(app.get('channel-rules-state').textContent,/暂时读不到/);
 assert.equal(app.get('channel-rules').textContent,'');
 assert.equal(app.get('channel-rules-raw').textContent,'');
 app.control.failedPaths.clear();
 Object.assign(app.control.routes['/observe/channel-runtime'],{core_enabled:false,policy:null,gate_words:null,disk_matches:null});
 app.get('runtime-refresh').events.click();await settle();
 assert.match(app.get('channel-rules-state').textContent,/未接入核心/);
 assert.equal(app.get('channel-rules').textContent,'');
});
