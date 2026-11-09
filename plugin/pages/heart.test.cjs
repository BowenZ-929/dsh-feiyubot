const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/heart.html','utf8');
const script=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const settle=()=>new Promise(resolve=>setImmediate(resolve));
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.value='';this.width=720;this.height=200;}
  set textContent(value){this.content=String(value);this.children=[];}
  get textContent(){return this.content||'';}
  appendChild(child){this.children.push(child);return child;}
  insertBefore(child,before){this.children.splice(before?this.children.indexOf(before):0,0,child);}
  get firstChild(){return this.children[0];}
  getContext(){this.drawing??=[];return new Proxy({}, {get:(_,name)=>(...args)=>this.drawing.push([name,...args])});}
}
function setup({base='',phase='awake',thoughts=[],fail=[],trajectory=[]}={}){
  const nodes=new Map([...html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)].map(m=>[m[2],new Element(m[1])]));
  nodes.get('changes-group').value='all';
  const requests=[];
  const fixtures={
    '/heart/overview':{ok:true,pending:[],refusals:{items:[]}},
    '/heart/effective':{ok:true,axes:{},voice:{tone:'平常',direction:'稳',bans:[]}},
    '/heart/mood':{now:60,tier:'平常',baseline:55,direction:'稳',as_of:'2026-11-07T12:00:00+09:00',trajectory},
    '/heart/energy':{value:48,tier:'够用',hours:0,sleep_window:[23,8]},
    '/heart/sleep':{ok:true,rest:phase?{phase}:undefined,effective_window:[23,8]},
    '/heart/engage':{state:'说',hits:[],window:{weight:0,consecutive_ignored:0,rows:0,window_min:60}},
    '/heart/pending':{thoughts},
    '/heart/refusals':{ok:true,quota:{used_today:0,limit:3,left:3}},
    '/heart/changes':{ok:true,date:'2026-11-07',timezone:'Asia/Tokyo',total:21,offset:0,has_older:true,available:true,rows:[
      {at:'2026-11-07T12:00:00+09:00',reason:'<img src=x> 他受到夸奖',source:'praise',source_line:1,changes:{mood:{before:55,after:60}}}]},
  };
  const context={URL,location:new URL('http://local'+base+'/heart'),window:{addEventListener:()=>{}},
    document:{querySelector:selector=>nodes.get(selector.slice(1)),createElement:tag=>new Element(tag),currentScript:{getAttribute:()=> 'heart'}},
    fetch:async(path,init)=>{requests.push({path,init});const route=path.replace(base,'').split('?')[0];
      if(fail.includes(route))throw Error('offline');return {json:async()=>fixtures[route]};}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/panel-base.js','utf8'),context);
  context.panelFetch=context.window.panelFetch;
  vm.runInNewContext(script,context);
  return {get:id=>nodes.get(id),requests,fixtures};
}
const text=node=>[node?.textContent||'',...(node?.children||[]).map(text)].join('\n');

test('mood curve uses elapsed time instead of equal sample spacing, including unordered rows',async()=>{
  const app=setup({trajectory:[
    {at:'2026-11-07T13:00:00+09:00',now:60,baseline:50},
    {at:'2026-11-07T12:00:00+09:00',now:40,baseline:50},
    {at:'2026-11-07T12:01:00+09:00',now:80,baseline:50},
  ]});await settle();
  const calls=app.get('curve').drawing;
  const first=calls.findIndex(c=>c[0]==='save');
  const line=calls.slice(first).filter(c=>c[0]==='moveTo'||c[0]==='lineTo').slice(0,3);
  assert.equal(line.length,3);
  assert.ok(Math.abs((line[1][1]-line[0][1])/(line[2][1]-line[0][1])-1/60)<1e-8);
  const labels=calls.filter(c=>c[0]==='fillText').map(c=>c[1]).join(' ');
  assert.match(labels,/11\/07/);
  assert.match(text(app.get('traj-info')),/实际时间间隔/);
});

test('one timestamp or invalid samples never become a fabricated trend',async()=>{
  for(const trajectory of [[],[{at:'bad',now:80,baseline:50}],
    [{at:'2026-11-07T12:00:00+09:00',now:80,baseline:50}],
    [{at:'2026-11-07T12:00:00+09:00',now:80,baseline:50},{at:'2026-11-07T12:00:00+09:00',now:60,baseline:50}]]){
    const app=setup({trajectory});await settle();
    assert.match(text(app.get('traj-info')),/不足两个不同时间/);
    assert.equal(app.get('curve').drawing.filter(c=>c[0]==='save').length,0);
  }
});

test('newest selected change is expanded with its evidence; older pages remain compact',async()=>{
 const app=setup();await settle();
 assert.equal(app.get('changes-list').children[0].children[0].open,true);
 app.fixtures['/heart/changes'].offset=20;
 app.get('changes-older').onclick();await settle();
 assert.equal(app.get('changes-list').children[0].children[0].open,false);
});

test('overview uses actual rest, not the permissive dry run or scheduled window',async()=>{
  for(const [phase,label] of [['awake','醒着'],['asleep','睡着'],['holding','被叫醒后硬撑'],['deep','彻底睡着'],[null,'尚未开始记录']]){
    const app=setup({phase});await settle();
    assert.equal(app.get('summary-rest')?.textContent,label);
    assert.equal(app.get('summary-mood')?.textContent,'平常');
    assert.equal(app.get('summary-energy')?.textContent,'够用');
    assert.equal(app.get('summary-voice')?.textContent,'平常');
    assert.match(text(app.get('engage')),/干跑/);
    assert.ok(app.requests.every(r=>!r.init||!r.init.method||r.init.method==='GET'));
  }
});

test('pending history distinguishes settled, faded, expired but unsettled, and unknown lifetime',async()=>{
  const app=setup({thoughts:[
    {kind:'wanted_praise',state:'open',ttl_left_min:12,attempts:1},
    {kind:'wanted_rest',state:'settled',ttl_left_min:20,attempts:0,settled_at:'2026-11-07T11:00:00+09:00'},
    {kind:'wanted_axis',axis:'他提到的名字',state:'faded',ttl_left_min:0,attempts:3,faded_at:'2026-11-07T11:30:00+09:00'},
    {kind:'wanted_rest',state:'open',ttl_left_min:0,expired:true,attempts:2},
    {kind:'wanted_rest',state:'open',ttl_left_min:null,expired:false,attempts:0},
    {kind:'wanted_praise',state:'open',ttl_left_min:0,expired:false,attempts:0},
  ]});await settle();
  assert.match(text(app.get('thoughts')),/还剩 12 分钟/);
  assert.match(text(app.get('thoughts')),/时限未知/);
  assert.match(text(app.get('thoughts')),/还剩 0 分钟/);
  assert.equal(app.get('thought-history').children.length,3);
  const history=text(app.get('thought-history'));
  assert.match(history,/已满足/);assert.match(history,/已淡去/);
  assert.match(history,/已到期，尚未结算/);
  assert.match(history,/他提到的名字/);
  assert.match(history,/2026-11-07T11:00:00/);
  assert.doesNotMatch(history,/自己缓过来|你提到的名字/);
});

for(const base of ['', '/qqagent/panel'])test('changes keep filtering, pagination and literal source text at '+base,async()=>{
  const app=setup({base});await settle();
  assert.equal(app.get('model-input-link').href,base+'/observe?source=model');
  assert.match(text(app.get('changes-list')),/<img src=x> 他受到夸奖/);
  app.get('changes-older').onclick();await settle();
  assert.match(app.requests.at(-1).path,/offset=20/);
  app.get('changes-group').value='sleep';app.get('changes-group').onchange();await settle();
  assert.equal(app.requests.at(-1).path,base+'/heart/changes?group=sleep&offset=0&date=2026-11-07');
  assert.ok(app.requests.every(r=>r.path.startsWith(base+'/heart/')&&!r.init));
});

test('unavailable overview reads do not look like normal or sleeping states',async()=>{
  const app=setup({fail:['/heart/sleep','/heart/mood','/heart/energy','/heart/effective']});await settle();
  for(const id of ['summary-rest','summary-mood','summary-energy','summary-voice'])assert.equal(app.get(id)?.textContent,'读取失败');
});

for(const base of ['', '/qqagent/panel'])test('emotion origin links exact turn and keeps old records unassociated at '+base,async()=>{
  const app=setup({base});await settle();
  assert.match(text(app.get('changes-list')),/未保存回合关联/);
  app.fixtures['/heart/changes'].rows[0].origin={turn_id:'a'.repeat(32),who:'qq:乙',scene:'group',
    evidence:'他说“<img src=x>”',note:'模型判断此句针对她'};
  app.get('changes-refresh').onclick();await settle();
  const all=node=>[node,...node.children.flatMap(all)];
  const links=all(app.get('changes-list')).filter(n=>n.tagName==='a');
  assert.equal(links.length,1);
  assert.equal(links[0].href,base+'/observe?source=model&turn_id='+'a'.repeat(32));
  const shown=text(app.get('changes-list'));
  assert.match(shown,/说话人：qq:乙/);assert.match(shown,/群聊/);
  assert.match(shown,/他说“<img src=x>”/);assert.match(shown,/模型判断此句针对她/);
  assert.doesNotMatch(shown,/未保存回合关联/);
});

test('proactive history explains waiting and cleared deadlines without treating null as missing history',async()=>{
 const app=setup();await settle();
 app.fixtures['/heart/changes']={ok:true,date:'2026-11-07',timezone:'JST',total:1,offset:0,available:true,rows:[{
  at:'2026-11-07T12:00:00+09:00',reason:'主动消息收到回应',group:'proactive',source:'proactive_replied',source_line:1,
  changes:{等待本次回应:{before:true,after:false},暂停主动开口至:{before:'2026-11-07T18:00:00+09:00',after:null},连续未获回应次数:{before:2,after:0}}}]};
 app.get('changes-group').value='proactive';app.get('changes-group').onchange();await settle();
 const content=text(app.get('changes-list'));
 assert.match(content,/等待本次回应：是 → 否/);assert.match(content,/→ 未设置暂停/);
 assert.match(content,/连续未获回应次数：2 → 0/);assert.doesNotMatch(content,/首次记录|true|false|proactive_replied/);
 assert.ok(app.requests.some(r=>r.path.includes('group=proactive')));
 assert.ok(html.includes('<option value="proactive">主动交流反馈</option>'));
 assert.ok(app.requests.every(r=>!r.init?.method||r.init.method==='GET'));
});
