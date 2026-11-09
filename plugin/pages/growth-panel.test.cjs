const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const settle=()=>new Promise(r=>setImmediate(r));
test('explains carried usage without inventing current activities',async()=>{
  const {elements:e}=setup(false,{usage:{day:'2026-11-09',used:11,remaining:13,accounting_only:11},log:[]});
  await settle();
  assert.match(e.get('growth-state').textContent,/其中 11 次为重养前已用额度/);
  assert.match(e.get('growth-log').textContent,/还没有新模式的活动记录/);
});
function setup(fail=false, overrides={}, base="", withLimits=true){
  const elements=new Map(), requests=[];
  const ids=['growth-state','growth-current','growth-plans','growth-log','growth-toggle','growth-refresh',
    'growth-field','growth-value','growth-note','growth-save','growth-version','growth-restore','growth-versions'];
  if(withLimits)ids.push('growth-limits','growth-interval','growth-cap','growth-limits-save');
  for(const id of ids) elements.set(id,{textContent:'',value:'',disabled:false,events:{},children:[],
    appendChild(n){this.children.push(n);},replaceChildren(){this.children=[];},addEventListener(k,f){this.events[k]=f;}});
  elements.get('growth-field').value='likes';
  const data={settings:{enabled:false},profile:{likes:'薄荷'},versions:[{version:1,profile:{likes:'薄荷'},note:'自己选的'}],
    intentions:[{name:'写诗',status:'paused',next_step:'明天再写'}],log:[],interval_minutes:120,daily_cap:2,phone_available:false};
  Object.assign(data, overrides);
  const control={fail};
  const context={window:{PANEL_BASE:base},document:{getElementById:id=>elements.get(id)||null,createElement:tag=>({tag,textContent:'',value:'',children:[],appendChild(n){this.children.push(n);}})},
    panelFetch:async(path,init={})=>{requests.push({path,init});return {ok:!control.fail,status:control.fail?503:200,json:async()=>control.fail?{message:'暂不可用'}:data};}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/growth-panel.js','utf8'),context);
  return {elements,requests,data,control};
}
test('limits save keeps growth disabled and rereads effective settings',async()=>{
  const {elements:e,requests,data}=setup();await settle();
  assert.equal(e.get('growth-interval').value,'120');
  assert.equal(e.get('growth-cap').value,'2');
  e.get('growth-interval').value='15';e.get('growth-cap').value='8';
  data.interval_minutes=30;data.daily_cap=10; // 独立模拟服务端回读值，页面必须以此为准。
  await e.get('growth-limits').events.submit({preventDefault(){}});
  assert.deepEqual(JSON.parse(requests[1].init.body),{enabled:false,interval_minutes:15,daily_cap:8});
  assert.equal(requests[1].path,'/day/growth/config');
  assert.equal(requests[2].init.method,'GET');
  assert.equal(e.get('growth-interval').value,'30');assert.equal(e.get('growth-cap').value,'10');
  assert.equal(requests.length,3);
});
test('failed limits save keeps unsaved input for retry, failed initial read blocks saving',async()=>{
  const {elements:e,requests,control}=setup();await settle();
  control.fail=true;e.get('growth-cap').value='12';
  await e.get('growth-limits').events.submit({preventDefault(){}});
  assert.match(e.get('growth-state').textContent,/没有保存/);
  assert.equal(e.get('growth-cap').value,'12');assert.equal(e.get('growth-limits-save').disabled,false);
  assert.equal(requests.length,2);
  const offline=setup(true);await settle();
  assert.equal(offline.elements.get('growth-limits-save').disabled,true);
  await offline.elements.get('growth-limits').events.submit({preventDefault(){}});
  assert.equal(offline.requests.length,1);
});
test('shared growth script works on soul page without limits form',async()=>{
  const {elements:e}=setup(false,{},'',false);await settle();
  assert.match(e.get('growth-state').textContent,/自主成长未开启/);
  assert.equal(e.get('growth-toggle').disabled,false);
});
test('shows current traits, intentions and limits; initial read does not enable',async()=>{
  const {elements:e,requests}=setup();await settle();
  assert.match(e.get('growth-current').textContent,/薄荷/);
  assert.match(e.get('growth-plans').textContent,/暂停.*明天再写/);
  assert.match(e.get('growth-state').textContent,/120.*2/);
  assert.equal(requests.length,1);assert.equal(requests[0].init.method,'GET');
});
test('failed activity shows the specific explanation and old rows keep fallback',async()=>{
 const {elements:e}=setup(false,{log:[
  {at:'2026-11-08T12:00:00',action:'write',status:'failed',error:'ValueError',error_detail:'完成需要指定自己的打算'},
  {at:'2026-11-07T12:00:00',status:'failed',error:'ValueError'}]});
 await settle();
 assert.match(e.get('growth-log').textContent,/write · 完成需要指定自己的打算/);
 assert.match(e.get('growth-log').textContent,/2026\/11\/7.*选择机会 · ValueError/);
});
test('correction and rollback send only named management fields',async()=>{
  const {elements:e,requests}=setup();await settle();
  e.get('growth-value').value='薄荷茶';e.get('growth-note').value='纠正';
  await e.get('growth-save').events.click();
  assert.deepEqual(JSON.parse(requests[1].init.body),{patch:{likes:'薄荷茶'},note:'纠正'});
  e.get('growth-version').value='1';await e.get('growth-restore').events.click();
  const restore=requests.find(r=>r.init.body && JSON.parse(r.init.body).version===1);
  assert.deepEqual(JSON.parse(restore.init.body),{version:1,note:'纠正'});
});
test('failed read stays unknown and disables toggle',async()=>{
  const {elements:e}=setup(true);await settle();
  assert.match(e.get('growth-state').textContent,/暂不可用/);
  assert.equal(e.get('growth-toggle').disabled,true);
});

test('shows actual phone connection reason without disabling growth controls',async()=>{
  for(const [available,reason] of [[false,'手机未连接'],[true,'手机已连接；打开时检查锁屏']]) {
    const {elements:e}=setup(false,{phone_available:available,phone:{available,reason}});await settle();
    assert.ok(e.get('growth-state').textContent.includes(reason));
    assert.equal(e.get('growth-toggle').disabled,false);
  }
});

for (const base of ['', '/qqagent/panel']) test('version opens exact model round under '+(base||'direct path'),async()=>{
  const versions=Array.from({length:10},(_,i)=>({version:i+1,profile:{likes:'<img src=x onerror=alert(1)>'},note:'理由'+i,sources:['工作台-1']}));
  versions[0].model_turn_id='a'.repeat(32);
  const {elements:e,requests}=setup(false,{versions},base);await settle();
  const history=e.get('growth-versions').children;
  assert.equal(history.length,10);
  const nodes=history.flatMap(v=>v.children);
  const link=nodes.find(n=>n.tag==='a');
  assert.equal(link.href,base+'/observe?source=model&turn_id='+'a'.repeat(32));
  assert.equal(nodes.filter(n=>n.tag==='a').length,1);
  assert.ok(nodes.some(n=>n.textContent.includes('<img src=x onerror=alert(1)>')));
  assert.ok(nodes.some(n=>n.textContent.includes('未关联模型记录')));
  assert.equal(requests.length,1);
});

test('sleep notice shows actual default and remains editable',async()=>{
 const {elements:e}=setup();await settle();
 assert.match(e.get('growth-current').textContent,/睡眠提示：睡觉啦，明天说/);
 const field=e.get('growth-field');field.value='sleep_notice';field.events.change.call(field);
 assert.equal(e.get('growth-value').value,'睡觉啦，明天说');
});

test('growth versions and choices display the same instant in browser local time',async()=>{
 const utc='2026-11-08T18:30:41.246413+00:00';
 const local='2026-11-09T03:30:41.246413+09:00';
 const versions=[{version:1,ts:utc,profile:{stance:'慢慢聊'},note:'自己的决定'}];
 const {elements:e}=setup(false,{versions,log:[{at:local,action:'idle',reason:'歇会儿'}]});
 await settle();
 const shown=new Date(utc).toLocaleString('zh-CN',{hour12:false});
 assert.ok(e.get('growth-versions').children[0].children[0].textContent.includes(shown));
 assert.ok(e.get('growth-log').textContent.includes(shown));
 assert.equal(versions[0].ts,utc);
});

test('version changes distinguish first record, clearing, restoring defaults and unchanged snapshots',async()=>{
 const versions=[
  {version:1,profile:{likes:'薄荷',stance:'慢慢聊'},note:'第一次'},
  {version:2,profile:{likes:'',stance:'慢慢聊',sleep_notice:'明天见'},note:'改变'},
  {version:3,profile:{likes:'薄荷',stance:'慢慢聊'},note:'回到第一版'},
  {version:4,profile:{likes:'薄荷',stance:'慢慢聊'},note:'历史相同快照'}];
 const before=JSON.stringify(versions);
 const {elements:e,requests}=setup(false,{versions});await settle();
 const records=e.get('growth-versions').children;
 const changes=records.map(r=>r.children.find(n=>n.className==='growth-version-changes')?.textContent);
 assert.equal(changes[0],'本次没有性情字段变化。');
 assert.match(changes[1],/成长兴趣：已清空 → 薄荷/);
 assert.match(changes[1],/睡眠提示：明天见 → 未设置（沿用初始或默认）/);
 assert.match(changes[2],/成长兴趣：薄荷 → 已清空/);
 assert.match(changes[2],/睡眠提示：未设置（沿用初始或默认） → 明天见/);
 assert.doesNotMatch(changes[2],/待人倾向/);
 assert.match(changes[3],/首次成长记录/);
 assert.match(changes[3],/成长兴趣：薄荷/);
 assert.doesNotMatch(changes[3],/→/);
 assert.equal(JSON.stringify(versions),before);
 assert.equal(requests.length,1);assert.equal(requests[0].init.method,'GET');
});
