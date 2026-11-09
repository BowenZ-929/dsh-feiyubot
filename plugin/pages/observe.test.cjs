// Executes the real page scripts with an isolated read-only HTTP/DOM boundary.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/observe.html','utf8');
const source=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const turn='a'.repeat(32);
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.textContent='';this._value='';}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;this._value='';}
  set value(value){this._value=value;}
  get value(){return this.tagName==='select'&&!this.children.some(c=>c.value===this._value)?'':this._value;}
}

for(const base of ['', '/qqagent/panel'])for(const called of [true,false])test('person context is attributed supplementation under '+base+' called='+called,async()=>{
  const memory={selected:[{id:'t-apology',reason:called?'person_context':'fixed_receipt',matched_by:['person_context'],in_prompt:true,sent_to_model:called,prompt_chars:30}],
    skipped:[],injected_chars:0,prompt_cap:900,reply_model_called:called};
  const app=setup(base+'/observe?source=chat',{memory});await settle();
  const text=node=>[node.textContent||'',...(node.children||[]).map(x=>typeof x==='string'?x:text(x))].join('\n');
  const cards=text(app.get('records'));
  assert.match(cards,/按人回读的近期相处/);
  assert.match(cards,/普通召回段正文 0 \/ 900/);
  assert.match(cards,/不参与相关性排名/);
  assert.doesNotMatch(cards,/第 未知 位|匹配依据未记录|分数 未记录/);
  assert.match(cards,called?/已交给回复模型/:/未交给回复模型/);
  const links=[];const walk=node=>{if(node.href)links.push(node.href);for(const child of node.children||[])if(typeof child!=='string')walk(child);};walk(app.get('records'));
  assert.ok(links.includes(base+'/galaxy?memory=event%3At-apology'));
});
function setup(path='/observe?source=chat',{empty=false,delivery,memory,participation,proactive=false,interpretation,tone,decision}={}){
  let url=new URL('http://local'+path);
  const nodes=new Map([...html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)].map(m=>[m[2],new Element(m[1])]));
  for(const [,id,body] of html.matchAll(/<select id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)){
    for(const [,value] of body.matchAll(/<option value="([^"]+)"/g)){const option=new Element('option');option.value=value;nodes.get(id).append(option);}
    nodes.get(id).value=nodes.get(id).children[0]?.value||'';
  }
  const requests=[],events=[],control={fail:false};
  const context={URL,URLSearchParams,Event,location:url,document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag),currentScript:{getAttribute:()=> 'observe'}},
    history:{replaceState(_s,_t,href){url=new URL(href,url);context.location=url;}},
    window:{dispatchEvent(event){events.push(event.type);}},
    fetch:async(path,init)=>{
      requests.push({path,init});
      if(path.includes('/observe/rice'))return {ok:false,json:async()=>({message:'测试不查询饭账'})};
      if(control.fail)return {ok:false,status:503,json:async()=>({message:'测试连接失败'})};
      if(path.includes('/observe/participation'))return {ok:true,json:async()=>participation||{
        ok:true,date:'2026-11-07',timezone:'JST',exists:false,sample_every:10,groups:[],invalid_lines:0,
        total:{segments:0,sent:0,spontaneous_sent:0,spontaneous_pct:null,total_pct:null,directed_pct:null}}};
      const query=new URL(path,'http://local').searchParams,offset=Number(query.get('offset')||0),kind=query.get('source');
      const record=kind==='chat'?{turn_id:turn,said:'虚构苹果',reply:'虚构回复'}:{turn_id:turn,reply:'虚构模型正文'};
      if(proactive)Object.assign(record,{mode:'proactive',who:'10000420',said:''});
      if(memory)record.memory_recall=memory;
      if(interpretation)record.interpretation=interpretation;
      if(tone)Object.assign(record,tone);
      if(decision)Object.assign(record,decision);
      return {ok:true,json:async()=>({ok:true,source:kind,turn_id:query.get('turn_id'),date:query.get('date')||(kind==='model'?'2026-11-07':''),dates:empty?[]:['2026-11-07','2026-11-06'],session_id:query.get('session_id')||'',sessions:['qq:group:7','qq:private:42'],offset,total:empty?0:41,exists:!empty,has_older:!empty&&offset<40,rows:empty?[]:[{line:1,record,delivery}]})};
    }};
  vm.runInNewContext(fs.readFileSync(__dirname+'/panel-base.js','utf8'),context);
  context.panelFetch=context.window.panelFetch;
  vm.runInNewContext(source,context);
  return {get:id=>nodes.get(id),url:()=>url,requests,events,control,
    recordsRequests:()=>requests.filter(r=>r.path.includes('/observe/records')),
    async change(id,value,event='onchange'){nodes.get(id).value=value;nodes.get(id)[event]();await settle();},
    async click(id){nodes.get(id).onclick();await settle();}};
}

test('sleep silence is explained from evidence while raw decision remains visible',async()=>{
  const text=node=>[node.textContent||'',...(node.children||[]).map(x=>typeof x==='string'?x:text(x))].join('\n');
  for(const [reason,label] of [['rest-deep','彻底睡着'],['rest-asleep','正在睡觉'],['rest-holding','硬撑期间'],[null,'没有可发送的回复']]){
    const app=setup('/observe?source=decision',{decision:{human:'没回：她这轮不想说',
      machine:{decided:'silent',reason_code:'core_empty',reason}}});await settle();
    const card=app.get('records').children[0];
    assert.match(text(card.children[card.children.findIndex(x=>text(x).includes('判断结果'))]),new RegExp(label));
    assert.match(text(card),/她这轮不想说/,'原始账保留，不能重写历史');
  }
  const app=setup('/observe?source=decision',{participation:{ok:true,date:'2026-11-07',timezone:'JST',exists:true,
    total:{segments:4,spontaneous_sent:1,spontaneous_pct:25},groups:[{target:'group:7',segments:4,
      spontaneous_sent:1,spontaneous_pct:25,directed:0,directed_sent:0,sent:1,
      silent_reasons:{'rest-deep':1,chitchat_skipped:1,core_error:1}}]}});await settle();
  const rendered=text(app.get('participation-groups'));
  assert.match(rendered,/彻底睡着.*1 段/);
  assert.match(rendered,/本段未抽中.*1 段/);
  assert.match(rendered,/核心调用出错.*1 段/);
});

for(const base of ['', '/qqagent/panel'])for(const length of ['sentence_clip','prompt_budget',null])test('tone distinguishes recorded controls from legacy knobs under '+base+' '+length,async()=>{
  const tone={address:'测试称呼',max_chars:220,softness:0.7,no_question:true,ban_joke:true,
    truncated:false,dropped_questions:0,dropped_jokes:0};
  if(length)tone.controls={length,questions:'not_filtered',jokes:'prompt_only'};
  const app=setup(base+'/observe?source=tone',{tone});await settle();
  const text=node=>[node.textContent||'',...(node.children||[]).map(x=>typeof x==='string'?x:text(x))].join('\n');
  const cards=text(app.get('records'));
  assert.match(cards,/测试称呼/);assert.match(cards,/软度参考/);
  assert.match(cards,/实际处理方式/);
  if(length){
    assert.match(cards,length==='sentence_clip'?/超预算时按句末裁剪，完整首句可能超预算/:/只写入提示词，不据此截断/);
    assert.match(cards,/不按问号删句/);assert.match(cards,/只作提示，不按词删句/);
  }else assert.match(cards,/旧记录未记处理方式/);
});

for(const base of ['', '/qqagent/panel'])test('group participation reads whole-day totals separately from record pagination under '+base,async()=>{
  const participation={ok:true,date:'2026-11-07',timezone:'JST',exists:true,sample_every:10,invalid_lines:2,
    total:{segments:100,sent:13,spontaneous_sent:10,spontaneous_pct:10,total_pct:13},groups:[{
      target:'group:123',segments:100,sent:13,spontaneous_sent:10,spontaneous_pct:10,
      directed:4,directed_sent:3,directed_pct:75,total_pct:13,other_sent:0,unconfirmed:1,proactive_sent:2}]};
  const app=setup(base+'/observe?source=decision&offset=20',{participation});await settle();
  const text=node=>[node.textContent||'',...(node.children||[]).map(x=>typeof x==='string'?x:text(x))].join('\n');
  assert.equal(app.get('participation').hidden,false);
  assert.match(text(app.get('participation-groups')),/10\.0%/);
  assert.match(text(app.get('participation-groups')),/75\.0%/);
  assert.match(text(app.get('participation-groups')),/13\.0%/);
  assert.match(text(app.get('participation-groups')),/未确认 1 段/);
  assert.match(text(app.get('participation-groups')),/另有主动分享 2 次/);
  assert.match(app.get('participation-note').textContent,/2/);
  await app.change('participation-date','2026-11-06');
  const request=app.requests.filter(r=>r.path.includes('/observe/participation')).at(-1);
  assert.ok(request.path.startsWith(base+'/observe/participation?'));
  assert.equal(new URL(request.path,'http://local').searchParams.get('date'),'2026-11-06');
  assert.ok(!request.path.includes('offset='));
  app.control.fail=true;await app.click('participation-refresh');
  assert.equal(app.get('participation-groups').children.length,0);
  assert.match(app.get('participation-status').textContent,/读取失败/);
  app.control.fail=false;await app.click('participation-refresh');
  assert.equal(app.get('participation-groups').children.length,1);
  await app.change('source','chat');
  assert.equal(app.get('participation').hidden,true);
});

test('missing participation records show absence rather than zero-percent success',async()=>{
  const app=setup('/observe?source=decision');await settle();
  assert.match(app.get('participation-status').textContent,/没有发言账/);
  assert.equal(app.get('participation-groups').children.length,0);
});

test('source read shows verbatim excerpt and failure separately without replacing original audit',async()=>{
  const memory={selected:[{id:'t-read',rank:1,reason:'included_excerpt',sent_to_model:true,source_read:{status:'ok',source_chars:1300,
    spans:[{field:'quote',start:100,end:110,text:'<img src=x>原话。'}]}},
    {id:'t-fail',rank:2,reason:'prompt_budget',source_read:{status:'invalid',source_chars:1400}}],
    skipped:[],injected_chars:200,prompt_cap:900,reply_model_called:true};
  const before=JSON.stringify(memory),app=setup('/observe?source=chat',{memory});await settle();
  const text=node=>[node.textContent||'',...(node.children||[]).map(x=>typeof x==='string'?x:text(x))].join('\n');
  const shown=text(app.get('records'));
  assert.match(shown,/回读后交给回复模型的逐字摘录/);
  assert.match(shown,/第 101–110 字/);
  assert.match(shown,/<img src=x>原话。/);
  assert.match(shown,/摘录未通过核对/);
  assert.match(shown,/不保证相关信息已选全/);
  assert.equal(JSON.stringify(memory),before);
});

for(const base of ['', '/qqagent/panel'])test('restores page/search and native turn link under '+(base||'core'),async()=>{
  const app=setup(base+'/observe?source=chat&offset=20&q=苹果');await settle();
  assert.equal(new URL(app.recordsRequests()[0].path,'http://local').searchParams.get('offset'),'20');
  assert.equal(app.get('search').value,'苹果');
  assert.match(app.get('status').textContent,/当前 21–40/);
  const link=app.get('records').children[0].children.find(n=>n.tagName==='a');
  assert.ok(link,'turn detail must be a native navigable link');
  assert.equal(link.href,base+'/observe?source=model&turn_id='+turn);
});

test('pagination, local search and refresh persist without making search an API query',async()=>{
  const app=setup();await settle();await app.click('older');
  await app.change('search','苹果 & 茶','oninput');
  assert.equal(app.url().searchParams.get('offset'),'20');
  assert.equal(app.url().searchParams.get('q'),'苹果 & 茶');
  assert.equal(app.recordsRequests().length,2);
  assert.ok(!app.recordsRequests().at(-1).path.includes('q='));
  await app.click('refresh');assert.equal(app.url().searchParams.get('offset'),'20');
  assert.equal(new URL(app.recordsRequests().at(-1).path,'http://local').searchParams.get('offset'),'20');
  const restored=setup(app.url().pathname+app.url().search);await settle();
  assert.equal(restored.get('search').value,'苹果 & 茶');
  assert.match(restored.get('status').textContent,/当前 21–40/);
  assert.ok(app.events.includes('panel-routechange'));
});

for(const base of ['', '/qqagent/panel'])test('all ledger dates persist through refresh and can be cleared under '+base,async()=>{
  const app=setup(base+'/observe?source=chat&date=2026-11-06&offset=20');await settle();
  assert.equal(app.get('date-label').hidden,false);
  assert.equal(app.get('date').value,'2026-11-06');
  await app.click('refresh');
  assert.equal(new URL(app.recordsRequests().at(-1).path,'http://local').searchParams.get('date'),'2026-11-06');
  assert.equal(app.url().searchParams.get('offset'),'20');
  await app.change('source','eye');
  assert.equal(app.url().searchParams.get('date'),'2026-11-06');
  assert.equal(app.url().searchParams.get('offset'),null);
  await app.change('date','');
  assert.equal(app.url().searchParams.get('date'),null);
  assert.equal(app.get('date').value,'');
  assert.equal(new URL(app.recordsRequests().at(-1).path,'http://local').searchParams.get('date'),null);
});

test('date changes reset pagination and keep dates with no log selectable',async()=>{
  const app=setup('/observe?source=model&date=2026-11-01&offset=20');await settle();
  assert.equal(app.get('date').value,'2026-11-01');
  await app.change('date','2026-11-06');
  assert.equal(app.url().searchParams.get('date'),'2026-11-06');
  assert.equal(app.url().searchParams.get('offset'),null);
  await app.click('refresh');
  assert.equal(new URL(app.recordsRequests().at(-1).path,'http://local').searchParams.get('date'),'2026-11-06');
});

test('empty trace directory still displays and preserves the requested date',async()=>{
  const app=setup('/observe?source=model&date=2026-11-01',{empty:true});await settle();
  assert.equal(app.get('date').value,'2026-11-01');
  assert.equal(app.url().searchParams.get('date'),'2026-11-01');
  assert.equal(app.get('records').children.length,0);
  assert.equal(app.get('status').textContent,'还没有这份记录。');
  assert.equal(app.get('older').disabled,true);
});

test('turn query is cross-date; unsupported sources remove hidden filters',async()=>{
  const app=setup('/observe?source=model&date=2026-11-01&turn_id='+turn+'&offset=20');await settle();
  assert.equal(app.url().searchParams.get('date'),null);
  assert.equal(app.get('date-label').hidden,true);
  await app.change('source','eye');
  assert.equal(app.url().searchParams.get('turn_id'),null);
  assert.equal(app.url().searchParams.get('source'),'eye');
  await app.change('source','model');await app.change('turn','');
  assert.equal(app.url().searchParams.get('turn_id'),null);
  assert.equal(app.get('date-label').hidden,false);
});

for(const base of ['', '/qqagent/panel'])test('same turn connects chat, emotion, tone and model under '+base,async()=>{
  const app=setup(base+'/observe?source=chat&turn_id='+turn);await settle();
  for(const source of ['chat','emotion','tone','model']){
    await app.change('source',source);
    assert.equal(app.get('turn-label').hidden,false);
    assert.equal(app.url().searchParams.get('turn_id'),turn);
    assert.equal(new URL(app.recordsRequests().at(-1).path,'http://local').searchParams.get('turn_id'),turn);
    const links=[];const walk=n=>{if(n.href)links.push(n.href);for(const c of n.children||[])if(typeof c!=='string')walk(c);};walk(app.get('records'));
    for(const other of ['chat','emotion','tone','model'].filter(x=>x!==source))assert.ok(links.includes(base+'/observe?source='+other+'&turn_id='+turn));
    await app.click('refresh');
    assert.equal(app.url().searchParams.get('turn_id'),turn);
  }
  await app.change('source','tone');
  const restored=setup(app.url().pathname+app.url().search);await settle();
  assert.equal(restored.get('turn').value,turn);
  assert.doesNotMatch(restored.get('status').textContent,/按调用顺序/);
  assert.equal(restored.get('older').textContent,'较早记录');
});

test('failed page load preserves requested location and retry recovers it',async()=>{
  const app=setup();await settle();app.control.fail=true;await app.click('older');
  assert.equal(app.url().searchParams.get('offset'),'20');
  assert.match(app.get('status').textContent,/读取失败/);
  assert.equal(app.get('refresh').disabled,false);
  app.control.fail=false;await app.click('refresh');
  assert.match(app.get('status').textContent,/当前 21–40/);
});

for(const offset of ['NaN','-20','1.5','Infinity'])test('invalid page offset defaults to zero: '+offset,async()=>{
  const app=setup('/observe?source=unknown&offset='+offset);await settle();
  const params=new URL(app.recordsRequests()[0].path,'http://local').searchParams;
  assert.equal(params.get('offset'),'0');assert.equal(params.get('source'),'model');
});

test('proactive chat shows its recipient without inventing a human utterance',async()=>{
  const app=setup('/observe?source=chat',{proactive:true,delivery:{state:'confirmed',text:'想去书店。'}});await settle();
  const blocks=app.get('records').children[0].children.filter(n=>n.tagName==='details');
  const origin=blocks.find(n=>n.children[0].textContent==='她主动开口');
  assert.match(origin.children[1].textContent,/对象：10000420/);
  assert.match(origin.children[1].textContent,/没有对方的新发言/);
  assert.ok(!blocks.some(n=>n.children[0].textContent==='10000420 说'));
  assert.equal(blocks.find(n=>n.children[0].textContent==='QQ 回执确认的文字').children[1].textContent,'想去书店。');
});

test('chat shows actual receipt text separately from its recorded draft',async()=>{
  const app=setup('/observe?source=chat',{delivery:{state:'confirmed',text:'实际确认发出的文字'}});await settle();
  const blocks=app.get('records').children[0].children.filter(n=>n.tagName==='details');
  const received=blocks.find(n=>n.children[0].textContent==='QQ 回执确认的文字');
  assert.ok(received,'receipt needs a distinct label');
  assert.equal(received.children[1].textContent,'实际确认发出的文字');
  const original=blocks.find(n=>n.children[0].textContent==='底稿里的回复');
  assert.equal(original.children[1].textContent,'虚构回复');
  await app.change('search','实际确认发出的文字','oninput');
  assert.equal(app.get('records').children.length,1,'visible receipt text must be searchable on this page');
});

for(const [delivery,message] of [
  [{state:'pending'},'尚无发送结果回执'],
  [{state:'unrecorded'},'没有可关联的 QQ 回执'],
  [undefined,'没有可关联的 QQ 回执'],
  [{state:'unavailable'},'回执读取失败'],
  [{state:'confirmed',text:''},'回执没有确认任何文字'],
])test('does not claim whole-round delivery for '+JSON.stringify(delivery),async()=>{
  const app=setup('/observe?source=chat',{delivery});await settle();
  const blocks=app.get('records').children[0].children.filter(n=>n.tagName==='details');
  assert.ok(blocks.some(n=>n.children[1].textContent.includes(message)));
  assert.ok(blocks.every(n=>!n.children[1].textContent.includes('整轮发送成功')));
});

for(const text of ['', '已发的第一句'])test('superseded reply explains unsent remainder: '+text,async()=>{
  const app=setup('/observe?source=chat',{delivery:{state:'confirmed',text,stop_reason:'superseded'}});await settle();
  const card=app.get('records').children[0];
  assert.ok(card.children.some(n=>n.tagName==='p' && n.textContent.includes('收到补充')));
  const blocks=card.children.filter(n=>n.tagName==='details');
  const received=blocks.find(n=>n.children[0].textContent==='QQ 回执确认的文字');
  assert.equal(received.children[1].textContent,text || '收到补充，旧稿剩余部分已停发，没有确认发出的文字。');
});

test('memory context links explain provenance without claiming budget-skipped text was sent',async()=>{
  const memory={selected:[{id:'t-new',rank:1,reason:'included',matched_by:['context_link'],context_from:['t-old'],sent_to_model:true}],
    skipped:[{id:'t-later',rank:7,reason:'budget_full',matched_by:['context_link'],context_from:['t-new']}],
    prompt_cap:900,injected_chars:200,reply_model_called:true};
  const app=setup('/observe?source=chat',{memory});await settle();
  const block=app.get('records').children[0].children.find(n=>n.tagName==='details' && n.children[0].textContent==='这轮记忆取舍');
  const text=block.children[1].textContent;
  assert.ok(text.includes('承接前文'));
  assert.ok(text.includes('关联来源：t-old'));
  assert.ok(text.includes('关联来源：t-new'));
  assert.ok(text.includes('总名额已满'));
});

for(const base of ['', '/qqagent/panel'])for(const [status,label] of [
 ['requested','已提出检索词'], ['not_requested','本轮没有请求检索'],
 ['missing','模型没有返回选图字段'], ['invalid','选图字段格式不正确'],
 ['unavailable','理解调用未完成'], ['disabled','本轮未开放聊天选图'],
 [undefined,'旧记录没有保存检索判断']
])test('meme search status is distinct and does not claim delivery: '+base+':'+status,async()=>{
 const app=setup(base+'/observe?source=emotion',{interpretation:{meme_search_status:status,meme_query:status==='requested'?'吃饭 开心':null}});await settle();
 const card=app.get('records').children[0];
 const block=card.children.find(n=>n.tagName==='details' && n.children[0].textContent==='聊天表情检索');
 assert.ok(block);
 assert.ok(block.children[1].textContent.includes(label));
 assert.ok(block.children[1].textContent.includes('不代表已选择或发送图片'));
 assert.ok(app.requests.every(r=>!r.init?.method || r.init.method==='GET'));
});

test('proactive interpretation is not mislabeled as a legacy missing record',async()=>{
 const app=setup('/observe?source=emotion',{interpretation:{status:'not_applicable',note:'主动开口，没有新的人际事件'}});await settle();
 const block=app.get('records').children[0].children.find(n=>n.tagName==='details' && n.children[0].textContent==='聊天表情检索');
 assert.ok(block.children[1].textContent.includes('本轮主动开口，未执行聊天表情检索'));
 assert.ok(!block.children[1].textContent.includes('旧记录'));
});

for(const base of ['', '/qqagent/panel'])test('session selection is server filtered and preserved in URL under '+base,async()=>{
 const app=setup(base+'/observe?source=chat&session_id=qq%3Aprivate%3A42&offset=20');await settle();
 const params=()=>new URL(app.recordsRequests().at(-1).path,'http://local').searchParams;
 assert.equal(params().get('session_id'),'qq:private:42');
 assert.equal(app.get('session').value,'qq:private:42');
 assert.equal(app.get('session-label').hidden,false);
 assert.ok(app.get('session').children.some(o=>o.textContent==='群聊 · 7'));
 await app.click('refresh');assert.equal(params().get('offset'),'20');
 await app.change('turn',turn);await app.change('session','qq:group:7');
 assert.equal(params().get('session_id'),'qq:group:7');assert.equal(params().get('offset'),'0');
 assert.equal(params().has('turn_id'),false);
 assert.equal(app.url().searchParams.get('session_id'),'qq:group:7');
 await app.change('source','eye');
 assert.equal(app.get('session-label').hidden,true);assert.equal(params().has('session_id'),false);
 assert.equal(app.url().searchParams.has('session_id'),false);
 await app.change('source','model');assert.equal(params().get('session_id'),'qq:group:7');
 await app.change('session','');assert.equal(params().has('session_id'),false);
});

test('empty session results keep the requested selection available for clearing',async()=>{
 const app=setup('/observe?source=chat&session_id=qq%3Aprivate%3A99',{empty:true});await settle();
 assert.equal(app.get('session').value,'qq:private:99');
 await app.change('session','');
 assert.equal(app.url().searchParams.has('session_id'),false);
});
