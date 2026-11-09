const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/expressions.html','utf8');
const script=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.textContent='';this.value='';}
  append(...items){this.children.push(...items);}
  replaceChildren(...items){this.children=items;}
  set innerHTML(_){throw Error('Source material must remain text');}
}
const text=node=>[node.textContent,...node.children.map(text)].join('\n');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function setup(record){
  const nodes=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element('div')]));
  nodes.get('filter').value='all';
  const requests=[];
  vm.runInNewContext(script,{document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag)},
    panelFetch:async(path,init)=>{requests.push({path,method:init.method});return {ok:true,json:async()=>({records:[record]})};}});
  return {get:id=>nodes.get(id),requests};
}
const row={id:'a'.repeat(32),created_at:'2026-11-07T12:00:00+09:00',enabled:false,verification:'原句逐字核对',cards:[],
 source:{context:'虚构验收',at:'2026-11-07T11:00:00+09:00',provenance:'采样文字，不等于完整评论区',
 nodes:[{node:7,package:'tv.danmaku.bili',text:'<img src=x onerror=alert(1)> 原样保留'},
        {node:9,package:'tv.danmaku.bili',text:'同屏未选中的另一句评论'}]}};
test('renders the original sampled text and structured clip even when no cards were extracted',async()=>{
 const app=setup({...row,source:{...row.source,viewed_clip:JSON.stringify({scope:'三帧片段',observation:{subject:'纸船漂到桥洞'},audio_status:'partial',transcript:'桥下面有只猫',transcript_truncated:true})}});
 await settle();const body=text(app.get('records'));
 for(const value of ['纸船漂到桥洞','桥下面有只猫','同屏未选中的另一句评论','<img src=x onerror=alert(1)> 原样保留','三帧片段'])assert.ok(body.includes(value),value);
 assert.match(body,/截断/);assert.match(body,/机器转写/);assert.match(body,/B站/);
 assert.ok(app.get('records').children[0].children.some(n=>n.tagName==='details'&&!n.open));
 assert.deepEqual(app.requests,[{path:'/learning/expressions',method:'GET'}]);
});
test('keeps legacy plain text and rejected source evidence readable without enabling it',async()=>{
 const app=setup({...row,source_rejected:true,source:{...row.source,viewed_clip:'历史人工片段说明，不能补成完整视频'}});
 await settle();const body=text(app.get('records'));
 assert.match(body,/历史人工片段说明/);assert.match(body,/来源有误/);assert.match(body,/同屏未选中的另一句评论/);
 assert.ok(!body.includes('采用这组（'));
});
test('missing source records are explicit and never reconstructed',async()=>{
 const app=setup({...row,source:{context:'旧记录',at:row.created_at,provenance:'旧来源'}});await settle();
 const body=text(app.get('records'));assert.match(body,/未保存视频片段语境/);assert.match(body,/未保存当时的页面文字/);
});
test('search can find visible source evidence without another request',async()=>{
 const app=setup(row);await settle();app.get('search').value='同屏未选中';app.get('search').oninput();
 assert.match(text(app.get('records')),/同屏未选中的另一句评论/);
 assert.equal(app.requests.length,1);
});
test('shows exact related browsing records and model links without interpreting titles as HTML',async()=>{
 const app=setup({...row,browse_sessions:[
  {id:'first',created_at:row.created_at,title:'<img src=x onerror=alert(1)> 浏览标题',status:'done',model_turn_id:'turn&one'},
  {id:'second',created_at:row.created_at,title:'再次遇到',status:'partial',model_turn_id:''}
 ]});await settle();const body=text(app.get('records'));
 assert.match(body,/关联浏览记录/);assert.match(body,/<img src=x onerror=alert\(1\)> 浏览标题/);assert.match(body,/再次遇到/);
 const walk=n=>[n,...n.children.flatMap(walk)];
 const links=walk(app.get('records')).filter(n=>n.tagName==='a');
 assert.deepEqual(links.map(n=>n.href),['observe?source=model&turn_id=turn%26one']);
 assert.match(body,/未关联模型调用/);
});
test('legacy expression has no guessed browser provenance',async()=>{
 const app=setup(row);await settle();assert.match(text(app.get('records')),/没有找到关联浏览记录/);
});
test('distinguishes offered references from delivered matches and links the actual chat',async()=>{
 const app=setup({...row,cards:[{id:'card',phrase:'这波稳了',offered_count:4,sent_match_count:1,
  last_sent_at:row.created_at,last_sent_turn_id:'turn&one'}]});await settle();
 const body=text(app.get('records'));
 assert.match(body,/提供给回复模型 4 次/);assert.match(body,/送达原句匹配 1 次/);
 assert.match(body,/不含改写用法/);
 const walk=n=>[n,...n.children.flatMap(walk)];
 assert.deepEqual(walk(app.get('records')).filter(n=>n.tagName==='a').map(n=>n.href),
  ['observe?source=chat&turn_id=turn%26one']);
 assert.deepEqual(app.requests,[{path:'/learning/expressions',method:'GET'}]);
});
test('shows recorded video source only for successful copy, without guessing old URLs',async()=>{
 const app=setup({...row,browse_sessions:[
  {id:'new',status:'done',created_at:row.created_at,video_source:{status:'ok',url:'https://b23.tv/X6f5iyP'}},
  {id:'failed',status:'done',created_at:row.created_at,video_source:{status:'unavailable',url:'https://b23.tv/old',reason:'复制未确认'}},
  {id:'old',status:'done',created_at:row.created_at}
 ]});await settle();
 const walk=n=>[n,...n.children.flatMap(walk)];
 const links=walk(app.get('records')).filter(n=>n.tagName==='a');
 assert.deepEqual(links.map(n=>n.href),['https://b23.tv/X6f5iyP']);
 assert.match(text(app.get('records')),/复制未确认/);
 assert.match(text(app.get('records')),/未取得原视频链接/);
});
test('links a reference turn even without a delivered original phrase',async()=>{
 const app=setup({...row,cards:[{id:'card',phrase:'这波稳了',offered_count:1,sent_match_count:0,
  last_offered_turn_id:'reference&one'}]});await settle();
 const walk=n=>[n,...n.children.flatMap(walk)];
 const links=walk(app.get('records')).filter(n=>n.tagName==='a');
 assert.deepEqual(links.map(n=>[n.textContent,n.href]),
  [['查看最近参考回合','observe?source=chat&turn_id=reference%26one']]);
 assert.match(text(app.get('records')),/不代表实际说过/);
 assert.match(text(app.get('records')),/送达原句匹配 0 次/);
});

test('filters undecided cards separately from declined or manually disabled cards',async()=>{
 for(const [fields,filter,label] of [
  [{},'pending','尚未判断'],
  [{selection_source:'model',review:{adopt:false,reason:'不适合这个场合'}},'inactive','未采用'],
  [{selection_source:'user'},'inactive','未采用'],
 ]){
  const app=setup({...row,cards:[{id:'card',phrase:'状态验收表达',enabled:false,...fields}]});
  await settle();
  assert.match(text(app.get('records')),new RegExp(label));
  app.get('filter').value=filter;app.get('filter').onchange();
  assert.match(text(app.get('records')),/状态验收表达/);
  app.get('filter').value=filter==='pending'?'inactive':'pending';app.get('filter').onchange();
  assert.doesNotMatch(text(app.get('records')),/状态验收表达/);
  assert.equal(app.requests.length,1);
 }
});

test('empty extraction and rejected sources are not undecided learning',async()=>{
 for(const item of [row,{...row,source_rejected:true,cards:[{id:'card',phrase:'错误来源'}]}]){
  const app=setup(item);await settle();
  app.get('filter').value='pending';app.get('filter').onchange();
  assert.match(text(app.get('records')),/没有符合条件的表达/);
 }
});
