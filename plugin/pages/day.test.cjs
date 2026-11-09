// Execute the actual day page; HTTP boundaries are read-only fixtures.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/day.html','utf8');
const script=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const settle=()=>new Promise(resolve=>setImmediate(resolve));
for(const base of ['', '/qqagent/panel'])test('memory history keeps full edits distinct from old summaries under '+base,async()=>{
 const activity={ok:true,date:'2026-11-07',sources:{people:'人物名片变化',memory_edit:'记忆理解修改'},errors:{},available:{people:true,memory_edit:true},total:3,rows:[
  {source:'people',id:'people:1',status:'edited',title:'修改人物名片',person_id:'10000011',actor:'panel',summary_only:true,before_summary:'旧摘要…',after_summary:'新摘要…'},
  {source:'people',id:'people:2',status:'edited',title:'修改人物名片',person_id:'10000011',actor:'panel',summary_only:false,changes:{备注:{before:[],after:['<img> 一整段备注的结尾']}}},
  {source:'memory_edit',id:'memory_edit:1',status:'edited',title:'修改记忆理解',entry_id:'t-sample',actor:'panel',changes:{note:{before:'旧理解',after:''},unclear:{before:false,after:true}},link:'galaxy?memory=event%3At-sample'}]};
 const app=setup(base,null,activity,null,{},'?source=people&date=2026-11-07');await settle();
 const content=text(app.get('activity-list'));
 for(const value of ['人物：10000011','操作者：panel','旧记录只保存摘要','改前摘要：旧摘要…','一整段备注的结尾','记忆：t-sample','记忆理解：旧理解 → 空值','不确定标记：无 → 有'])assert.ok(content.includes(value),value);
 assert.equal(app.get('activity-source').value,'people');
 const links=[];function visit(n){if(n.tagName==='a')links.push(n.href);for(const c of n.children)visit(c);}visit(app.get('activity-list'));
 assert.ok(links.some(link=>{const url=new URL(link,app.url);return url.pathname===base+'/galaxy'&&url.search==='?memory=event%3At-sample';}));
 assert.ok(app.requests.every(r=>!r.init?.method || r.init.method==='GET'));
});
test('proactive self-decline is distinguished from a failed or cancelled draft',async()=>{
 const activity={ok:true,date:'2026-11-09',sources:{proactive:'主动开口'},errors:{},available:{proactive:true},total:1,rows:[
  {id:'proactive:1',source:'proactive',title:'主动开口',stage:'due',status:'declined',
   correction_reason:'model-declined',after_generation:true,turn_id:'a'.repeat(32)}]};
 const app=setup('',null,activity);await settle();
 const content=text(app.get('activity-list'));
 assert.match(content,/她选择这次不主动开口，没有发送，也不会重试这条计划/);
 assert.doesNotMatch(content,/生成后已取消，草稿没有发送/);
});
test('proactive allowance shows planned opportunities and clears stale quota on failed refresh',async()=>{
 const routes={'/day/proactive':{ok:true,delivery:{enabled:true,routes:[{who:'3425635473',scene:'private',target:3425635473}]},
   quota:{used:2,cap:5,remaining:3,reasons:{own_doing:{used:1,cap:3}}}}};
 const app=setup('',null,null,null,routes);await settle();
 assert.match(text(app.get('proactive-quota')),/最多 5 次/);
 assert.match(text(app.get('proactive-quota')),/已使用 2 次.*剩余 3 次/);
 assert.match(text(app.get('proactive-quota')),/自身经历分享最多 3 次/);
 assert.match(text(app.get('proactive-quota')),/不等于已送达次数/);
 delete routes['/day/proactive'];
 await app.get('proactive-refresh').onclick();
 assert.doesNotMatch(text(app.get('proactive-quota')),/剩余 3 次/);
});
test('context history distinguishes background edits and renders image metadata readably',async()=>{
 const activity={ok:true,date:'2026-11-07',sources:{life:'生活背景设置与调整',art_assets:'画室素材变化'},errors:{},available:{life:true,art_assets:true},total:2,rows:[
  {source:'life',id:'life:1',status:'edited',title:'调整生活背景安排',day:'2026-11-08',revision:2,
   detail:'生活背景安排的修改，不代表实际经历。',changes:{outfit:{before:'蓝外套',after:'白外套'}}},
  {source:'art_assets',id:'art_assets:1',status:'edited',title:'修改画室素材',detail:'白色披风',revision:3,
   changes:{category:{before:'characters',after:'outfits'},images:{before:[{sha256:'old-hash',width:20}],after:[{sha256:'new-hash',width:40}]}}}]};
 const app=setup('',null,activity,null,{},'?source=life&date=2026-11-07');await settle();
 assert.equal(app.get('activity-source').value,'life');
 const content=text(app.get('activity-list'));
 for(const value of ['不代表实际经历','安排日期：2026-11-08','保存为第 2 版','衣着：蓝外套 → 白外套','参考图片','old-hash','new-hash','保存为第 3 版'])assert.ok(content.includes(value),value);
 assert.doesNotMatch(content,/\[object Object\]/);
 assert.match(text(app.get('activity-list').children[1].children[0].children[0]),/白色披风/);
 assert.match(content,/素材类别：人物 → 服饰/);

 const assets=setup('',null,activity,null,{},'?source=art_assets&date=2026-11-07');await settle();
 assert.equal(assets.get('activity-source').value,'art_assets');
 assert.ok(app.requests.every(r=>!r.init?.method||r.init.method==='GET'));
});
test('model setting history shows thinking and request limits as before and after',async()=>{
 const activity={ok:true,date:'2026-11-09',sources:{model_settings:'模型设置变化'},errors:{},available:{model_settings:true},total:1,rows:[
  {id:'model_settings:1',source:'model_settings',status:'setting',title:'修改主模型调用参数',changes:{
   thinking:{before:{type:'disabled'},after:{type:'enabled'}},reasoning_effort:{before:'low',after:'high'},
   max_tokens:{before:400,after:8192}},link:'settings#model-card'}]};
 const app=setup('',null,activity,null,{},'?source=model_settings&date=2026-11-09');await settle();
 assert.equal(app.get('activity-source').value,'model_settings');
 const content=text(app.get('activity-list'));
 assert.match(content,/思考开关：关闭 → 开启/);assert.match(content,/思考档位：low → high/);
 assert.match(content,/输出 token 上限：400 → 8192/);
});
test('soul history distinguishes baseline summaries, missing values and rollback versions',async()=>{
 const activity={ok:true,date:'2026-11-07',sources:{soul:'初始设定变化'},errors:{},available:{soul:true},total:1,rows:[
  {id:'soul:1',source:'soul',status:'rollback',title:'回滚初始设定',actor:'人工调整',field:'manner.stance',
   before_summary:null,after_summary:'<img> 慢慢形成',revision:4,target_revision:2,link:'./#history-list'}]};
 const app=setup('',null,activity);await settle();
 const content=text(app.get('activity-list'));
 for(const expected of ['初始设定的历史','可能截断','改前摘要：未记录','改后摘要：<img> 慢慢形成',
   '操作者：人工调整','manner.stance','保存为第 4 版','回滚目标：第 2 版'])assert.ok(content.includes(expected),expected);
 assert.ok(app.requests.every(r=>!r.init?.method || r.init.method==='GET'));
});
test('meme history shows chat switch, edited descriptions and import count',async()=>{
 const activity={ok:true,date:'2026-11-07',sources:{meme:'表情图库变化'},errors:{},available:{meme:true},
  offset:0,limit:20,total:3,rows:[
   {source:'meme',id:'meme:3',status:'edit',title:'修改表情资料',changes:{description:{before:'旧描述',after:'新描述'}}},
   {source:'meme',id:'meme:2',status:'settings',title:'修改聊天选图开关',changes:{chat_enabled:{before:false,after:true}}},
   {source:'meme',id:'meme:1',status:'import',title:'导入表情图库',imported:276}]};
 const app=setup('',null,activity,null,{},'?source=meme&date=2026-11-07');await settle();
 assert.equal(app.get('activity-source').value,'meme');
 const content=text(app.get('activity-list'));
 assert.match(content,/聊天选图开关：关闭 → 开启/);
 assert.match(content,/图片描述：旧描述 → 新描述/);
 assert.match(content,/导入 276 张表情/);
});
test('activity settings show interval and allowance with readable units',async()=>{
  const activity={ok:true,date:'2026-11-07',sources:{growth_settings:'自主活动设置'},errors:{},available:{growth_settings:true},
    offset:0,limit:20,total:1,rows:[{source:'growth_settings',id:'growth_settings:1',status:'setting',
      at:'2026-11-07T12:00:00+09:00',title:'修改自主活动设置',changes:{
        interval_minutes:{before:120,after:30},daily_cap:{before:2,after:24}}}]};
  const app=setup('',null,activity);await settle();
  const content=text(app.get('activity-list'));
  assert.match(content,/机会间隔（分钟）：120 → 30/);
  assert.match(content,/每生活日最多（次）：2 → 24/);
});
test('activity timeline shows nonverbal clues and classifier failure without claiming silence',async()=>{
 const activity={ok:true,date:'2026-11-07',sources:{ear:'听语音'},errors:{},available:{ear:true},total:2,rows:[
  {id:'ear:1',source:'ear',title:'听语音',status:'empty',sound_events:{status:'ok',processed_seconds:8,events:[{label:'音乐',score:.73}]}},
  {id:'ear:2',source:'ear',title:'听语音',status:'ok',transcript:'下午见',sound_events:{status:'failed',processed_seconds:2,events:[]}}]};
 const app=setup('',null,activity);await settle();
 const content=text(app.get('activity-list'));
 assert.match(content,/可能有音乐/);
 assert.match(content,/前 8 秒/);
 assert.match(content,/本次声音分类失败/);
 assert.match(content,/下午见/);
 assert.doesNotMatch(content,/已确认静音/);
});
test('proactive configuration shows recipient changes as readable text without sending',async()=>{
 const activity={ok:true,date:'2026-11-07',sources:{proactive:'主动开口'},errors:{},available:{proactive:true},total:1,rows:[
  {id:'proactive:1',source:'proactive',title:'主动开口',stage:'config',status:'enabled',changes:{
    routes:{before:[{scene:'private',target:123456}],after:[{scene:'private',target:987654}]},
    enabled:{before:false,after:true},enabled_at:{before:null,after:'2026-11-07T09:00:00+09:00'},
    per_who_daily_cap:{before:3,after:5},own_doing_daily_cap:{before:1,after:3}}}]};
 const app=setup('',null,activity);await settle();
 const content=text(app.get('activity-list'));
 assert.match(content,/接收会话：私聊 QQ 123456 → 私聊 QQ 987654/);
 assert.match(content,/功能开关：关闭 → 开启/);
 assert.match(content,/本次启用时间：未设置 →/);
 assert.match(content,/每生活日主动开口上限（次）：3 → 5/);
 assert.match(content,/其中自身经历分享上限（次）：1 → 3/);
 assert.doesNotMatch(content,/\[object Object\]/);

 assert.ok(app.requests.every(r=>!r.init?.method || r.init.method==='GET'));
});
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.textContent='';this.value='';}
  appendChild(child){this.children.push(child);return child;}
  addEventListener(type,handler){this['on'+type]=handler;}
  get firstChild(){return this.children[0];}
  removeChild(child){this.children.splice(this.children.indexOf(child),1);}
}
function setup(base,plan,activity,solitude,dayRoutes={},search=''){
  const nodes=new Map([...html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)].map(m=>[m[2],new Element(m[1])]));
  const requests=[],control={plan,solitude};
  nodes.get('activity-source').value='all';
  const context={URL,URLSearchParams,Event,location:new URL('http://local'+base+'/day'+search),window:{dispatchEvent(){}},
    document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag),currentScript:{getAttribute:()=> 'day'}},
    fetch:async(path,init)=>{requests.push({path,init});const fixture=dayRoutes[path.replace(base,'')];if(fixture)return {status:200,json:async()=>fixture};return {status:path.includes('/day/life')||path.includes('/observe/activity')&&activity||path.includes('/day/solitude')&&control.solitude?200:503,
      json:async()=>path.includes('/day/solitude')&&control.solitude?control.solitude:path.includes('/observe/activity')&&activity?activity:path.includes('/day/life')?{day:'2026-11-07',now:'2026-11-07T10:00:00+09:00',
        enabled:true,cutover_hour:4,plan:control.plan,rest:control.rest,current:control.current}:{message:'outside this test'}};}};
  context.history={replaceState(_state,_title,url){context.location.href=new URL(url,context.location).href;}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/panel-base.js','utf8'),context);
  context.panelFetch=context.window.panelFetch;
  vm.runInNewContext(script,context);
  return {get:id=>nodes.get(id),requests,control,url:context.location,
    async refresh(){nodes.get('life-refresh').onclick();await settle();}};
}
const text=node=>[node?.textContent||'',...(node?.children||[]).map(text)].join('\n');
const runs=[{turn_id:'a'.repeat(32),attempt:1,at:'2026-11-07T09:00:00+09:00'},
            {turn_id:'b'.repeat(32),attempt:2,at:'2026-11-07T09:05:00+09:00'}];
for(const base of ['', '/qqagent/panel'])test('phone settings timeline shows saved before/after without claiming an activity under '+base,async()=>{
  const activity={ok:true,date:'2026-11-07',sources:{phone_settings:'手机开关变化'},errors:{},available:{phone_settings:true},
    offset:0,limit:20,total:1,rows:[{source:'phone_settings',id:'phone_settings:1',status:'setting',
      title:'修改手机自主浏览开关',changes:{enabled:{before:true,after:false}},link:'phone'}]};
  const app=setup(base,null,activity,null,{},'?source=phone_settings&date=2026-11-07');await settle();
  assert.equal(app.get('activity-source').value,'phone_settings');
  assert.match(text(app.get('activity-list')),/设置已保存/);
  assert.match(text(app.get('activity-list')),/开启 → 关闭/);
  assert.doesNotMatch(text(app.get('activity-list')),/浏览流程完成|查看本次浏览的模型调用/);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});
for(const base of ['', '/qqagent/panel'])test('activity URL survives refresh and reopening under '+base,async()=>{
  const fixture={ok:true,date:'2026-11-07',sources:{chat:'聊天底稿'},errors:{},available:{chat:true},
    offset:20,limit:20,total:60,has_older:true,rows:[]};
  const app=setup(base,null,fixture,null,{},'?source=chat&date=2026-11-07&offset=20&include_tests=1#activity-card');
  await settle();
  const lastQuery=()=>new URL(app.requests.filter(r=>r.path.includes('/observe/activity?')).at(-1).path,'http://local').searchParams;
  assert.equal(lastQuery().get('source'),'chat');
  assert.equal(lastQuery().get('date'),'2026-11-07');
  assert.equal(lastQuery().get('offset'),'20');
  assert.equal(lastQuery().get('include_tests'),'1');
  app.get('activity-refresh').onclick();await settle();
  assert.equal(lastQuery().get('offset'),'20');
  app.get('activity-older').onclick();await settle();
  assert.equal(app.url.searchParams.get('offset'),'40');
  assert.equal(app.url.pathname,base+'/day');
  assert.equal(app.url.hash,'#activity-card');
  const reopened=setup(base,null,fixture,null,{},app.url.search+app.url.hash);await settle();
  assert.equal(reopened.get('activity-source').value,'chat');
  assert.equal(reopened.get('activity-tests').checked,true);
  assert.equal(reopened.url.searchParams.get('offset'),'40');
  app.get('activity-source').value='heart';app.get('activity-source').onchange();await settle();
  assert.equal(lastQuery().get('offset'),'0');
  assert.equal(app.url.searchParams.get('source'),'heart');
  assert.equal(app.url.searchParams.get('offset'),null);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});
test('invalid activity address options fall back to defaults',async()=>{
  const app=setup('',null,null,null,{},'?source=unknown&date=bad&offset=-20&include_tests=perhaps');await settle();
  const query=new URL(app.requests.find(r=>r.path.includes('/observe/activity?')).path,'http://local').searchParams;
  assert.equal(query.get('source'),'all');assert.equal(query.get('offset'),'0');
  assert.equal(query.get('include_tests'),'0');assert.equal(query.get('date'),null);
});
for(const base of ['', '/qqagent/panel'])test('phone activity links recorded model rounds only under '+base,async()=>{
  const activity={ok:true,date:'2026-11-07',sources:{phone:'手机浏览汇总'},errors:{},available:{phone:true},
    offset:0,limit:20,total:3,rows:[
      {source:'phone',id:'phone:a',status:'done',model_turn_id:'a'.repeat(32),link:'phone'},
      {source:'phone_event',id:'phone_event:a:1',status:'recorded',model_turn_id:'a'.repeat(32),link:'phone'},
      {source:'phone',id:'phone:old',status:'done',link:'phone'}]};
  const app=setup(base,null,activity);await settle();
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  const links=descendants(app.get('activity-list')).filter(n=>n.href?.includes('turn_id='));
  assert.equal(links.length,2);
  assert.ok(links.every(n=>new URL(n.href,app.url).pathname===base+'/observe'));
  assert.ok(links.every(n=>n.href.endsWith('turn_id='+'a'.repeat(32))));
});
for(const base of ['', '/qqagent/panel'])test('proactive control stops without generating or sending under '+base,async()=>{
  const routes=[{who:'10000420',scene:'private',target:10000420}];
  const app=setup(base,null,null,null,{'/day/proactive':{ok:true,delivery:{enabled:true,routes}},
    '/day/proactive/disable':{ok:true,delivery:{enabled:false,routes}}});await settle();
  assert.equal(app.get('proactive-state').textContent,'已开启');
  assert.equal(app.get('proactive-stop').disabled,false);
  assert.match(app.get('proactive-routes').textContent,/私聊 10000420/);
  await app.get('proactive-stop').onclick();
  assert.equal(app.get('proactive-state').textContent,'已关闭');
  assert.equal(app.get('proactive-stop').disabled,true);
  const writes=app.requests.filter(r=>r.init.method!=='GET');
  assert.equal(writes.length,1);assert.equal(writes[0].path,base+'/day/proactive/disable');
});
test('proactive stop is available before slow status loads and when status fails',async()=>{
  let release;
  const slow=new Promise(resolve=>{release=resolve;});
  const app=setup('',null,null,null,{'/day/proactive':slow,
    '/day/proactive/disable':{ok:true,delivery:{enabled:false,routes:[]}}});
  assert.equal(app.get('proactive-stop').disabled,false);
  await app.get('proactive-stop').onclick();
  assert.equal(app.get('proactive-state').textContent,'已关闭');
  release({ok:true,delivery:{enabled:false,routes:[]}});await settle();
  const failed=setup('',null,null,null,{'/day/proactive/disable':{ok:true,delivery:{enabled:false,routes:[]}}});
  await settle();
  assert.equal(failed.get('proactive-state').textContent,'读取失败');
  assert.equal(failed.get('proactive-stop').disabled,false);
  await failed.get('proactive-stop').onclick();
  assert.equal(failed.get('proactive-state').textContent,'已关闭');
});
for(const base of ['', '/qqagent/panel'])test('proactive recipient form enables only the specified private QQ under '+base,async()=>{
  const routes=[{who:'10000420',scene:'private',target:10000420}];
  const app=setup(base,null,null,null,{'/day/proactive':{ok:true,delivery:{enabled:false,routes:[]}},
    '/day/proactive/enable':{ok:true,delivery:{enabled:true,routes}}});
  assert.equal(app.get('proactive-enable').disabled,true);
  await settle();
  assert.equal(app.get('proactive-enable').disabled,false);
  app.get('proactive-qq').value='10000420';
  await app.get('proactive-form').onsubmit({preventDefault(){}});
  const writes=app.requests.filter(r=>r.init.method!=='GET');
  assert.equal(writes.length,1);assert.equal(writes[0].path,base+'/day/proactive/enable');
  assert.deepEqual(JSON.parse(writes[0].init.body),{private_qq:'10000420'});
  assert.equal(app.get('proactive-state').textContent,'已开启');
  assert.equal(app.get('proactive-stop').disabled,false);
  assert.match(app.get('proactive-save-msg').textContent,/已保存/);
});
test('proactive enable stays blocked after read failure and preserves input after save failure',async()=>{
  const unread=setup('',null,null,null);await settle();
  assert.equal(unread.get('proactive-enable').disabled,true);
  assert.equal(unread.get('proactive-stop').disabled,false);
  const app=setup('',null,null,null,{'/day/proactive':{ok:true,delivery:{enabled:false,routes:[]}}});await settle();
  app.get('proactive-qq').value='123456789';
  await app.get('proactive-form').onsubmit({preventDefault(){}});
  assert.equal(app.get('proactive-qq').value,'123456789');
  assert.equal(app.get('proactive-enable').disabled,false);
  assert.equal(app.get('proactive-stop').disabled,false);
  assert.match(app.get('proactive-save-msg').textContent,/未能确认开启/);
});
for(const base of ['', '/qqagent/panel'])test('proactive receipt and model trace remain distinct under '+base,async()=>{
  const activity={ok:true,date:'2026-11-07',sources:{proactive:'主动开口'},errors:{},available:{proactive:true},offset:0,limit:20,total:1,rows:[
    {source:'proactive',id:'proactive:1',status:'partial',stage:'ack',turn_id:'a'.repeat(32),confirmed_text:'实际送到的第一句',
     detail:'拟说的由头',plan_id:'p-one'}]};
  const app=setup(base,null,activity);await settle();
  const body=text(app.get('activity-list'));
  assert.match(body,/部分文字已确认/);
  assert.match(body,/实际送到的第一句/);
  const links=[];const walk=n=>{if(n.href)links.push(n.href);(n.children||[]).forEach(walk);};walk(app.get('activity-list'));
  assert.ok(links.some(h=>h.includes('observe?source=model&turn_id='+'a'.repeat(32))));
});
for(const base of ['', '/qqagent/panel'])test('generated but discarded draft is explicit in the timeline under '+base,async()=>{
  const activity={ok:true,date:'2026-11-07',sources:{proactive:'主动开口'},errors:{},available:{proactive:true},offset:0,limit:20,total:2,rows:[
    {source:'proactive',id:'proactive:1',status:'declined',stage:'due',after_generation:true},
    {source:'proactive',id:'proactive:2',status:'declined',stage:'due'}]};
  const app=setup(base,null,activity);await settle();
  assert.equal((text(app.get('activity-list')).match(/生成后由头变化，草稿未入队/g)||[]).length,1);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});
for(const base of ['', '/qqagent/panel'])test('state timeline keeps emotion author, quote and exact turn under '+base,async()=>{
  const origin={who:'群友甲',scene:'group',turn_id:'a'.repeat(32),
    evidence:'<img src=x onerror=alert(1)>',note:'对bot的赞赏'};
  const row={source:'heart',id:'heart:1',at:'2026-11-07T12:00:00+09:00',status:'recorded',
    group:'mood',group_label:'心情与基线',title:'时间回落后应用聊天情绪',trigger_source:'praise',
    changes:{心情:{before:60,after:64}},link:'heart'};
  const activity={ok:true,date:'2026-11-07',sources:{heart:'状态变化'},errors:{},available:{heart:true},
    offset:0,limit:20,total:2,rows:[{...row,emotion_origin:origin},{...row,id:'heart:2'}]};
  const app=setup(base,null,activity);await settle();
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  const rendered=text(app.get('activity-list'));
  assert.match(rendered,/说话人：群友甲/);assert.match(rendered,/群聊/);
  assert.ok(rendered.includes(origin.evidence));assert.ok(rendered.includes(origin.note));
  assert.match(rendered,/未保存回合关联/);
  const nodes=descendants(app.get('activity-list'));
  assert.equal(nodes.filter(n=>n.tagName==='img').length,0);
  const links=nodes.filter(n=>n.tagName==='a'&&n.href.includes('turn_id='));
  assert.equal(links.length,1);
  const url=new URL(links[0].href,app.url);
  assert.equal(url.pathname,base+'/observe');assert.equal(url.searchParams.get('turn_id'),origin.turn_id);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});
for(const base of ['', '/qqagent/panel'])test('life links retain each generation attempt and work under '+base,async()=>{
  const app=setup(base,{status:'failed',error:'测试失败',model_runs:runs});await settle();
  const box=app.get('life-models');
  assert.ok(box,'generation trace container exists');
  const links=box.children.flatMap(row=>row.children).filter(n=>n.tagName==='a');
  assert.equal(links.length,2);
  for(let i=0;i<2;i++){
    const url=new URL(links[i].href,app.url);
    assert.equal(url.pathname,base+'/observe');
    assert.equal(url.searchParams.get('turn_id'),runs[i].turn_id);
    assert.match(links[i].textContent,new RegExp('第 '+(i+1)+' 次'));
  }
  app.control.plan={status:'ready',title:'旧安排',items:[],changes:[],revision:1};
  await app.refresh();
  assert.match(text(box),/未保存.*关联/);
  assert.equal(box.children.flatMap(n=>n.children).filter(n=>n.tagName==='a').length,0);
  app.control.plan=null;await app.refresh();assert.equal(box.children.length,0);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

for(const base of ['', '/qqagent/panel'])test('night steps link failed and carried writing traces under '+base,async()=>{
  const activity={ok:true,date:'2026-11-07',sources:{night:'夜间整理'},errors:{},available:{night:true},offset:0,limit:20,total:1,rows:[{
    source:'night',id:'night:1',at:'2026-11-07T03:30:00+09:00',status:'recorded',day:'2026-11-06',attempt:1,
    retried_steps:['home_diary'],failed_steps:['home_diary'],steps:{
      home_diary:{status:'failed',model_turn_id:'a'.repeat(32)},
      home_dream:{status:'ok',model_turn_id:'b'.repeat(32)},brain_decay:{status:'stubbed'}}}]};
  const app=setup(base,null,activity);await settle();
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  const links=descendants(app.get('activity-list')).filter(n=>n.tagName==='a');
  assert.equal(links.length,2);
  assert.match(text(app.get('activity-list')),/沿用之前结果/);
  for(let i=0;i<2;i++){const url=new URL(links[i].href,app.url);
    assert.equal(url.pathname,base+'/observe');assert.equal(url.searchParams.get('turn_id'),['a','b'][i].repeat(32));}
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

test('solitude shows actual idle time separately from the next opportunity and sleep',async()=>{
  const app=setup('',null,null,{ok:true,at:'2026-11-07T14:01:00+09:00',night:false,
    mood_tier:'平常',energy_tier:'充沛',idle_minutes:141,threshold_minutes:40,
    last_decision_at:'2026-11-07T14:00:00+09:00',next_opportunity_at:'2026-11-07T14:40:00+09:00'});
  await settle();const box=app.get('solitude-info');assert.ok(box);
  assert.match(text(box),/141/);assert.match(text(box),/40/);assert.match(text(box),/14:40/);
  assert.match(app.get('solitude-state').textContent,/间隔/);
  app.control.solitude.night=true;app.get('solitude-refresh').onclick();await settle();
  assert.match(app.get('solitude-state').textContent,/休息/);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

for(const base of ['', '/qqagent/panel'])test('proactive source and unknown old queue remain distinct under '+base,async()=>{
  const source={entry_id:'t-source',who:'qq:200',scene:'group',at:'2026-11-07T10:00:00+09:00'};
  const app=setup(base,null,null,null,{
    '/day/pending':{counts:{expect:2},items:[
      {text:'接着你的话说',to:'qq:200',source,state:'expect'},
      {text:'旧稿',to:'qq:100',state:'expect'}]},
    '/day/plan':{pending:[{kind:'proactive',who:'qq:200',source,note:'原话'}]},
  });
  await settle();
  const pending=text(app.get('pending-list')),planned=text(app.get('plan-pending'));
  for(const rendered of [pending,planned]){
    assert.match(rendered,/拟回应对象：qq:200/);
    assert.match(rendered,/群聊（具体群未保存）/);
    assert.match(rendered,/原话编号 t-source/);
  }
  assert.match(pending,/未保存原话来源/);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

for(const base of ['', '/qqagent/panel'])test('known conversation stays attached to proactive source under '+base,async()=>{
  const source={entry_id:'t-source',who:'qq:200',scene:'group',session_id:'qq:group:70001'};
  const app=setup(base,null,null,null,{
    '/day/pending':{counts:{expect:1,delivered:0},items:[{text:'继续刚才的话题',to:'qq:200',source,state:'expect'}]},
    '/day/plan':{pending:[{kind:'proactive',who:'qq:200',source,note:'原话'}]},
  });
  await settle();
  for(const id of ['pending-list','plan-pending']){
    assert.match(text(app.get(id)),/来源会话 qq:group:70001/);
    assert.doesNotMatch(text(app.get(id)),/具体群未保存/);
    assert.match(text(app.get(id)),/拟回应对象：qq:200/);
  }
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

test('life distinguishes actual rest from the scheduled activity',async()=>{
  const item={start:'2026-11-07T22:00:00+09:00',end:'2026-11-08T04:00:00+09:00',activity:'看视频',detail:'原安排'};
  const app=setup('',{status:'ready',items:[item],changes:[],revision:1});
  app.control.current=item;
  for(const phase of ['asleep','holding','deep','awake','untracked']){
    app.control.rest={phase};await app.refresh();
    const state=text(app.get('life-state'));
    assert.match(state,/实际状态/);
    assert.equal(state.includes('聊天以休息状态为准'),['asleep','holding','deep'].includes(phase));
    assert.match(text(app.get('life-list')),/此时原安排 · 看视频/);
    assert.doesNotMatch(text(app.get('life-list')),/现在 · 看视频/);
  }
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});
for(const base of ['', '/qqagent/panel'])test('growth timeline distinguishes unchanged, cleared and queued with recorded model links '+base,async()=>{
  const activity={ok:true,date:'2026-11-07',sources:{autonomy:'自主活动',growth:'认识变化',night:'夜间整理'},errors:{},
    available:{autonomy:true,growth:true,night:true},offset:0,limit:20,total:4,rows:[
      {source:'autonomy',id:'a',status:'queued',event:'autonomy_result',action:'paint',title:'逐笔绘画',model_turn_id:'a'.repeat(32),link:'day#growth-card',intention_id:'plan-1'},
      {source:'growth',id:'b',status:'changed',title:'第 3 版自我认识',version:3,changes:{likes:{before:'薄荷',after:''},views:{before:'旧看法',after:null}},sources:['experience-1'],link:'./#growth-card'},
      {source:'night',id:'c',status:'unchanged',steps:{},failed_steps:[],title:'整理自己的认识',model_turn_id:'b'.repeat(32)},
      {source:'autonomy',id:'d',status:'idle',title:'这次闲着',link:'day#growth-card'}]};
  const app=setup(base,null,activity,null,{},'?source=autonomy&date=2026-11-07');await settle();
  const view=text(app.get('activity-list'));
  assert.equal(app.get('activity-source').value,'autonomy');
  assert.match(view,/已排队，等待实际结果/);assert.match(view,/保持当前认识/);
  assert.match(view,/喜欢：薄荷 → 已清空/);assert.match(view,/当前看法：旧看法 → 沿用初始设定/);
  assert.match(view,/experience-1/);assert.match(view,/plan-1/);
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  const links=descendants(app.get('activity-list')).filter(n=>n.href?.includes('turn_id='));
  assert.equal(links.length,2);
  assert.ok(links.every(n=>new URL(n.href,app.url).pathname===base+'/observe'));
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

for(const base of ['', '/qqagent/panel'])test('painting records link their actual model round under '+base,async()=>{
  const activity={ok:true,date:'2026-11-07',sources:{painting:'绘画'},errors:{},available:{painting:true},
    offset:0,limit:20,total:3,rows:[
      {source:'painting',id:'painting:a',status:'done',model_turn_id:'b'.repeat(32),link:'studio'},
      {source:'painting_event',id:'painting_event:a:1',status:'ok',model_turn_id:'b'.repeat(32),link:'studio'},
      {source:'painting',id:'painting:old',status:'done',link:'studio'}]};
  const app=setup(base,null,activity);await settle();
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  const links=descendants(app.get('activity-list')).filter(n=>n.href?.includes('turn_id='));
  assert.equal(links.length,2);
  assert.ok(links.every(n=>new URL(n.href,app.url).pathname===base+'/observe'));
  assert.ok(links.every(n=>n.href.endsWith('turn_id='+'b'.repeat(32))));
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});


test('painting reread keeps observation separate from writing and names original source',async()=>{
 const activity={ok:true,date:'2026-11-09',sources:{autonomy:'自主活动'},errors:{},available:{autonomy:true},total:1,rows:[
  {source:'autonomy',id:'reread',action:'read',status:'done',receipt:{kind:'painting',card_id:'工作台-301',job_id:'a'.repeat(32),based_on:'工作台-222',text:'不应重复显示原始JSON',observation:{subject:'白毛衣上有蓝色鲸鱼',uncertainty:'鞋未入画'}}}]};
 const app=setup('',null,activity);await settle();const content=text(app.get('activity-list'));
 for(const value of ['主体：白毛衣上有蓝色鲸鱼','未看清或未确认：鞋未入画','本次重新看图得到的观察','原画活动：工作台-222','原画未修改','画作编号：'+'a'.repeat(32)])assert.ok(content.includes(value),value);
 assert.doesNotMatch(content,/不应重复显示原始JSON/);
 assert.doesNotMatch(content,/接续原稿/);
 assert.doesNotMatch(content,/当时读到的作品/);
 assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

for(const base of ['', '/qqagent/panel'])test('autonomous receipts show actual artifacts without inventing old results under '+base,async()=>{
  const activity={ok:true,date:'2026-11-08',sources:{autonomy:'自主活动'},errors:{},available:{autonomy:true},offset:0,limit:20,total:7,rows:[
    {source:'autonomy',id:'write',action:'write',status:'done',receipt:{card_id:'工作台-208',text:'第一段。\n第二段<img src=x onerror=alert(1)>',based_on:'工作台-201'}},
    {source:'autonomy',id:'read',action:'read',status:'done',receipt:{id:'工作台-200',text:'当时读到的原稿',intention_id:'工作台-100'}},
    {source:'autonomy',id:'plan',action:'plan',status:'done',receipt:{card_id:'工作台-100',name:'写一首短诗',status:'paused',next_step:'明天再改最后一句'}},
    {source:'autonomy',id:'queued',action:'paint',status:'queued',receipt:{id:'paint-job',status:'running'}},
    {source:'autonomy',id:'failed',action:'paint',status:'failed',receipt:{card_id:'工作台-209',job_id:'failed-job'}},
    {source:'autonomy',id:'old',action:'write',status:'done'},
    {source:'autonomy',id:'idle',action:'idle',status:'idle'}]};
  const app=setup(base,null,activity);await settle();
  const children=app.get('activity-list').children,view=text(app.get('activity-list'));
  for(const value of ['第一段。\n第二段<img src=x onerror=alert(1)>','当时读到的原稿','工作台-201','工作台-208','工作台-200','写一首短诗','暂停','明天再改最后一句','paint-job','failed-job'])assert.ok(view.includes(value),value);
  assert.match(text(children[3]),/已排队，等待实际结果/);
  assert.doesNotMatch(text(children[3]),/作品已完成/);
  assert.match(text(children[4]),/本次失败/);
  assert.match(text(children[5]),/未保存结果回执/);
  assert.doesNotMatch(text(children[6]),/未保存结果回执/);
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  assert.equal(descendants(app.get('activity-list')).filter(n=>n.tagName==='img').length,0);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

for(const base of ['', '/qqagent/panel'])test('own activity feeling shows actual source and change under '+base,async()=>{
  const activity={ok:true,date:'2026-11-08',sources:{autonomy:'自主活动'},errors:{},available:{autonomy:true},offset:0,limit:20,total:3,rows:[
    {source:'autonomy',id:'positive',action:'idle',status:'idle',reaction:{id:'工作台-208',effect:'positive',impression:'写出纸船有点开心',evidence:'纸船<img src=x>',mood:{ok:true,before:50,after:51,delta:1}}},
    {source:'autonomy',id:'neutral',action:'idle',status:'idle',reaction:{id:'工作台-209',effect:'neutral',impression:'',evidence:'',mood:{ok:true,before:51,after:51,delta:0}}},
    {source:'autonomy',id:'old',action:'idle',status:'idle'}]};
  const app=setup(base,null,activity);await settle();
  const rows=app.get('activity-list').children;
  assert.match(text(rows[0]),/感受依据：工作台-208/);
  assert.match(text(rows[0]),/写出纸船有点开心/);
  assert.match(text(rows[0]),/纸船<img src=x>/);
  assert.match(text(rows[0]),/心情：50 → 51/);
  assert.match(text(rows[1]),/没有特别感受/);
  assert.match(text(rows[1]),/心情：51 → 51/);
  assert.doesNotMatch(text(rows[2]),/感受依据|没有特别感受/);
  const descendants=n=>[n,...n.children.flatMap(descendants)];
  assert.equal(descendants(app.get('activity-list')).filter(n=>n.tagName==='img').length,0);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

for(const base of ['', '/qqagent/panel'])test('autonomy separates choice and execution evidence under '+base,async()=>{
  const rows=[
    {job_id:'paint',status:'done',model_turn_id:'b'.repeat(32),mode:'strokes'},
    {job_id:'image',status:'done',mode:'image'},
    {job_id:'old',status:'done',mode:'strokes'},
    {job_id:'gone',status:'missing'},
    {job_id:'phone',status:'partial',model_turn_id:'c'.repeat(32)}
  ].map(execution=>({source:'autonomy',id:execution.job_id,event:'autonomy_result',status:'queued',
      action:execution.job_id==='phone'?'browse':'paint',model_turn_id:'a'.repeat(32),
      receipt:{id:execution.job_id},execution}));
  const app=setup(base,null,{ok:true,date:'2026-11-07',sources:{autonomy:'自主活动'},errors:{},available:{autonomy:true},
    offset:0,limit:20,total:rows.length,rows});await settle();
  const descendants=node=>[node,...node.children.flatMap(descendants)];
  const links=descendants(app.get('activity-list')).filter(n=>n.href?.includes('turn_id='));
  assert.equal(links.length,7);
  assert.equal(links.filter(n=>n.textContent==='查看实际执行的模型记录').length,2);
  assert.ok(links.every(n=>new URL(n.href,app.url).pathname===base+'/observe'));
  assert.ok(links.some(n=>n.href.endsWith('b'.repeat(32))));
  assert.ok(links.some(n=>n.href.endsWith('c'.repeat(32))));
  const content=text(app.get('activity-list'));
  assert.match(content,/执行任务当前状态：/);
  assert.match(content,/本地模型直接生图，没有语言模型规划与检查记录/);
  assert.match(content,/未保存执行模型记录编号/);
  assert.match(content,/执行任务记录已缺失/);
  assert.ok(app.requests.every(r=>r.init.method==='GET'));
});

for(const base of ['', '/qqagent/panel'])test('preference changes link person, original memory and model round '+base,async()=>{
 const activity={ok:true,date:'2026-11-09',sources:{preference:'相处偏好变化'},errors:{},available:{preference:true},total:2,rows:[
  {id:'preference:1',source:'preference',status:'updated',title:'调整相处偏好',person_id:'10000011',session_id:'qq:private:10000011',actor:'conversation',
   evidence:['以后别说<script>'],changes:{interaction_preference:{before:['过去说法'],after:['新的要求']}},missing_previous:['missing'],
   memory_links:[{id:'t-one',link:'galaxy?memory=event%3At-one'}],model_turn_id:'a'.repeat(32),link:'about?who=10000011#preferences-card'},
  {id:'preference:2',source:'preference',status:'withdrawn',title:'撤回相处偏好',person_id:'10000011',session_id:'qq:private:10000011',actor:'panel',
   evidence:[],changes:{interaction_preference:{before:['新的要求'],after:[]}},memory_links:[],missing_previous:[],link:'about?who=10000011#preferences-card'}]};
 const app=setup(base,null,activity,null,{},'?source=preference&date=2026-11-09');await settle();
 const content=text(app.get('activity-list'));
 for(const value of ['人物：10000011','qq:private:10000011','以后别说<script>','相处偏好：过去说法 → 新的要求','找不到 1 条被替换记录'])assert.ok(content.includes(value),value);
 const links=[];function visit(n){if(n.tagName==='a')links.push(new URL(n.href,app.url));for(const c of n.children)visit(c);}visit(app.get('activity-list'));
 assert.ok(links.some(u=>u.pathname===base+'/about'&&u.search==='?who=10000011'&&u.hash==='#preferences-card'));
 assert.ok(links.some(u=>u.pathname===base+'/galaxy'&&u.search==='?memory=event%3At-one'));
 assert.equal(links.filter(u=>u.pathname===base+'/observe'&&u.search==='?source=model&turn_id='+'a'.repeat(32)).length,1);
 assert.ok(app.requests.every(r=>!r.init?.method||r.init.method==='GET'));
});

test('proactive heart history renders actual cleared deadline and boolean state',async()=>{
 const activity={ok:true,date:'2026-11-07',sources:{heart:'状态变化'},errors:{},available:{heart:true},total:1,rows:[{
 source:'heart',id:'feedback',status:'recorded',group:'proactive',group_label:'主动交流反馈',title:'主动消息收到回应',trigger_source:'proactive_replied',
 changes:{等待本次回应:{before:true,after:false},暂停主动开口至:{before:'2026-11-07T18:00:00+09:00',after:null}}}]};
 const app=setup('',null,activity);await settle();const content=text(app.get('activity-list'));
 assert.match(content,/等待本次回应：是 → 否/);assert.match(content,/→ 未设置暂停/);
 assert.doesNotMatch(content,/true|false|null|无此前记录|proactive_replied/);
 assert.ok(app.requests.every(r=>r.init.method==='GET'));
});
