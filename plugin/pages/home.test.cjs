// DOM double for page logic only; this does not substitute for browser/layout QA.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/home.html','utf8');
const source=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.events={};this.style={};this.value='';this.disabled=false;this.text='';}
  set textContent(value){this.text=value;this.children=[];}
  get textContent(){return this.text+this.children.map(n=>n.textContent).join('');}
  get firstChild(){return this.children[0];}
  appendChild(node){this.children.push(node);return node;}
  removeChild(node){this.children.splice(this.children.indexOf(node),1);}
  addEventListener(event,fn){this.events[event]=fn;}
  setAttribute(name,value){this[name]=value;}
  removeAttribute(name){delete this[name];}
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function setup(health, web){
  const elements=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],new Element()]));
  const get=id=>elements.get(id);
  const rows=Array.from({length:247},(_,i)=>({card_id:'id-'+i,name:'同名日记',display:'篇目'+i,
    kind:i===246?'痕迹':'日记',shelf:i<225?'旧日记':'日记',params:{text:'摘要'+i,text_truncated:true,text_chars:400}}));
  rows[0].params.model_turn_id='c'.repeat(32);
  rows[0].params.review={draft:'原稿',changes:[{quote:'原稿',reason:'依据不足'}]};
  const requests=[]; const control={failList:false,failRead:false};
  vm.runInNewContext(source,{document:{getElementById:get,createElement:tag=>new Element(tag)},
    URLSearchParams,Date,localStorage:{getItem:()=>null,setItem(){}},
    panelFetch:async(path,init)=>{
      const url=new URL(path,'http://local');requests.push({url,method:init.method});
      let status=503,data={message:'离线'};
      if(url.pathname==='/learning/web'&&web){status=200;data=web;}
      if(url.pathname==='/healthz'&&health){status=200;data=health;}
      if(url.pathname==='/home/list'&&url.searchParams.get('room')==='抽屉'&&!control.failList){
        status=200;
        const shelf=url.searchParams.get('shelf'),offset=Number(url.searchParams.get('offset')||0),limit=Number(url.searchParams.get('limit')||200);
        const filtered=rows.filter(r=>!shelf||r.shelf===shelf);
        data={total:filtered.length,offset,limit,shelf,items:filtered.slice(offset,offset+limit),
          shelves:Object.fromEntries([...new Set(filtered.map(r=>r.shelf))].map(s=>[s,filtered.filter(r=>r.shelf===s).length]))};
      }
      if(url.pathname==='/home/read'&&!control.failRead){
        const card=rows.find(r=>r.card_id===url.searchParams.get('name')&&r.shelf===url.searchParams.get('shelf'));
        if(card){status=200;data={body:'全文-'+card.card_id+'<img src=x>',truncated:false,card};}
      }
      return {ok:status===200,status,json:async()=>data};
    }});
  return {get,requests,control,cards:()=>get('drawer-body').children.filter(n=>n.className==='drawer-card')};
}
test('drawer can reach every card past 200, jump/filter, and retain selection on refresh',async()=>{
  const {get,cards,requests}=setup();await settle();
  assert.ok(get('drawer-next'),'missing next-page control leaves cards past 200 unreachable');
  const names=[];
  while(true){
    names.push(...cards().map(c=>c.children[0].textContent));
    if(get('drawer-next').disabled)break;
    get('drawer-next').events.click();await settle();
  }
  assert.equal(names.length,247);assert.equal(new Set(names).size,247);
  assert.ok(names.includes('篇目246'),'unknown kinds must not vanish');
  assert.match(get('drawer-state').textContent,/247/);
  get('drawer-page').value='0';get('drawer-page').events.change();await settle();
  assert.equal(cards()[0].children[0].textContent,'篇目0');
  get('drawer-shelf').value='日记';get('drawer-shelf').events.change();await settle();
  assert.equal(cards()[0].children[0].textContent,'篇目225');
  get('refresh-all').events.click();await settle();
  assert.equal(cards()[0].children[0].textContent,'篇目225');
  assert.equal(get('drawer-shelf').children.length,3,'keep other shelves after filtering');
  assert.ok(requests.every(r=>r.method==='GET'));
});
test('full text resolves by card id AND shelf, preserves review, and read failure can retry',async()=>{
  const {cards,control}=setup();await settle();
  const card=cards()[0],button=card.children.find(n=>n.tag==='button');
  assert.ok(button,'truncated text needs a full-text control');
  control.failRead=true;button.events.click();await settle();
  assert.equal(button.disabled,false);
  assert.match(card.textContent,/失败/);
  control.failRead=false;button.events.click();await settle();
  assert.ok(card.children.some(n=>n.tag==='pre'&&n.textContent==='全文-id-0<img src=x>'));
  assert.match(card.textContent,/原稿/);assert.match(card.textContent,/依据不足/);
});
test('a failed page is not presented as an empty drawer and can reload',async()=>{
  const {get,control,cards}=setup();await settle();
  control.failList=true;get('refresh-all').events.click();await settle();
  assert.match(get('drawer-state').textContent,/失败/);
  assert.equal(get('refresh-all').disabled,false);
  control.failList=false;get('refresh-all').events.click();await settle();
  assert.ok(cards().length>0);
});

for(const changed of [true,false]) test('version footer separates source changes from an uncommitted tree: '+changed,async()=>{
  const app=setup({ok:true,source_changed:changed,dirty:true,stale:false,
    git_head:'abc',started_at:'2026-11-07T10:00:00Z',pid:123,
    source_digest:'aaaa',source_digest_now:changed?'bbbb':'aaaa',advice:'核对源码摘要'});
  await settle();
  assert.equal(app.get('build').className,changed?'build bad':'build');
  if(changed)assert.match(app.get('build').textContent,/bbbb/);
});

test('old-server footer does not repeat an unsupported old-code claim',async()=>{
  const app=setup({ok:true,dirty:true,stale:false,git_head:'abc',started_at:'2026-11-07T10:00:00Z',pid:123,
    source_digest:'aaaa',source_digest_now:'aaaa',advice:'重启核更保险（跑的是旧字节）'});
  await settle();
  assert.match(app.get('build').textContent,/尚未提供/);
  assert.doesNotMatch(app.get('build').textContent,/跑的是旧字节/);
  assert.equal(app.get('build').className,'build warn');
});

test('drawer links original writing trace without inventing one for legacy cards',async()=>{
  const app=setup();await settle();
  const links=app.cards().flatMap(card=>card.children).filter(n=>n.tag==='a');
  assert.equal(links.length,1);
  for(const base of ['', '/qqagent/panel']){
    const url=new URL(links[0].href,'http://local'+base+'/home');
    assert.equal(url.pathname,base+'/observe');assert.equal(url.searchParams.get('turn_id'),'c'.repeat(32));
  }
  assert.match(links[0].textContent,/写作.*模型记录/);
  assert.ok(app.requests.every(r=>r.method==='GET'));
});

test('public web cards show date and source as text, without treating HTML as content',async()=>{
  const {get}=setup(null,{enabled:true,jobs:[{cards:[{title:'<img src=x>',claim:'公开摘要'}],
    source:{query:'公共问题',url:'https://example.test/a',published_at:'2026-11-09'},fetched_at:'2026-11-09T12:00:00Z'}]});
  await settle();
  assert.match(get('web-state').textContent,/已开启/);
  assert.match(get('web-cards').textContent,/<img src=x>/);
  assert.match(get('web-cards').textContent,/页面时间：2026-11-09/);
  assert.equal(get('web-cards').children[0].children.at(-1).href,'https://example.test/a');
});
