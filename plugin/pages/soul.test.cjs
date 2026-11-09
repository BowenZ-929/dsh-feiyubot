// Run the actual page script with isolated HTTP/DOM doubles; browser QA is separate.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/soul.html','utf8');
const source=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const settle=()=>new Promise(resolve=>setImmediate(resolve));

function setup(failedPath, overrides={}){
  const elements=new Map(), requests=[];
  class Element {
    constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.events={};this.value='';this.disabled=false;this.text='';this.classList={add(){},remove(){}};}
    set id(value){this._id=value;elements.set(value,this);}
    get id(){return this._id;}
    set textContent(value){this.text=value;this.children=[];}
    get textContent(){return this.text+this.children.map(n=>n.textContent).join('');}
    get firstChild(){return this.children[0];}
    appendChild(node){node.parentNode=this;this.children.push(node);return node;}
    removeChild(node){this.children.splice(this.children.indexOf(node),1);}
    addEventListener(event,fn){this.events[event]=fn;}
    querySelector(){return null;}
  }
  for(const match of html.matchAll(/id="([^"]+)"/g)){const el=new Element();el.id=match[1];}
  const proposal=(id,status,value)=>({id,status,value,target:'manner.stance',reason:'虚构提案依据',evidence:['甲','乙','丙'],actor:'测试者',ts:'2026-11-07T08:00:00Z',decision_reason:'虚构拒绝理由'});
  const pending=proposal('pending','pending','待决定的新说法');
  const responses={
    '/heart/sleep':{ok:true,rest:{phase:'deep',wake_at:'2026-11-09T08:00:00+09:00'}},
    '/observe/channel':{ok:true,bots:[{business_connected:true,running:true}]},
    '/soul':{identity:{display:'虚构小鱼'},revision:15},
    '/soul/render':{text:'虚构注入正文'},
    '/soul/history':{history:[]},
    '/soul/check':{issues:[]},
    '/soul/revisions':{revisions:[{revision:14}]},
    '/soul/proposals?status=pending':{proposals:[pending]},
    '/soul/proposals?status=all':{proposals:[pending,proposal('accepted','accepted','已经接受的说法'),proposal('rejected','rejected','已经拒绝的说法')]}
  };
  Object.assign(responses,overrides);
  vm.runInNewContext(source,{document:{getElementById:id=>elements.get(id),createElement:tag=>new Element(tag)},
    window:{prompt:()=>{throw Error('Read-only test must not prompt');},confirm:()=>{throw Error('Read-only test must not confirm');}},
    panelFetch:async(path,init)=>{
      requests.push({path,method:init.method});
      assert.equal(init.method,'GET');assert.ok(path in responses,path);
      return {status:path===failedPath?503:200,json:async()=>path===failedPath?{message:'虚构读取失败'}:responses[path]};
    }});
  return {get:id=>elements.get(id),requests};
}

test('soul refresh routes revisions, pending and handled results to their own sections',async()=>{
  const {get,requests}=setup();await settle();
  assert.equal(requests.length,9);
  assert.doesNotMatch(get('identity-msg').textContent,/连不上|TypeError/);
  assert.equal(get('rollback-btn').disabled,false);
  assert.match(get('revision-now').textContent,/r15.*r14/);
  assert.equal(get('proposal-list').children.length,1);
  assert.match(get('proposal-list').textContent,/待决定的新说法/);
  assert.doesNotMatch(get('proposal-list').textContent,/已经接受|已经拒绝/);
  assert.equal(get('proposal-done-list').children.length,2);
  assert.match(get('proposal-done-list').textContent,/已经接受的说法/);
  assert.match(get('proposal-done-list').textContent,/已经拒绝的说法/);
});

test('overview distinguishes deep sleep from QQ connection and only reads',async()=>{
  const {get,requests}=setup();await settle();
  assert.equal(get('overview-rest').textContent,'彻底睡着了');
  assert.match(get('overview-wake').textContent,/计划醒来.*私聊提示一次/);
  assert.equal(get('overview-channel').textContent,'QQ 已连接');
  assert.equal(get('overview-refresh').disabled,false);
  await get('overview-refresh').events.click();
  assert.deepEqual(requests.slice(-2).map(r=>r.path),['/heart/sleep','/observe/channel']);
  assert.ok(requests.every(r=>r.method==='GET'));
});

test('overview failures do not imply awake or disconnect another working source',async()=>{
  const {get}=setup('/heart/sleep');await settle();
  assert.equal(get('overview-rest').textContent,'睡眠状态暂时读不到');
  assert.equal(get('overview-wake').textContent,'');
  assert.equal(get('overview-channel').textContent,'QQ 已连接');
  const unknown=setup(undefined,{'/heart/sleep':{ok:true,rest:{phase:'untracked'}},'/observe/channel':{ok:true,bots:[]}});
  await settle();
  assert.equal(unknown.get('overview-rest').textContent,'尚无睡眠记录');
  assert.equal(unknown.get('overview-channel').textContent,'尚无 QQ 连接');
});

for(const [path,message] of [
  ['/soul/revisions','rollback-msg'],
  ['/soul/proposals?status=pending','proposal-msg'],
  ['/soul/proposals?status=all','proposal-done-msg']
]) test('HTTP error stays in its own section: '+path,async()=>{
  const {get}=setup(path);await settle();
  assert.match(get(message).textContent,/HTTP 503.*虚构读取失败/);
  assert.doesNotMatch(get('identity-msg').textContent,/连不上|TypeError/);
  if(path!=='/soul/revisions')assert.equal(get('rollback-btn').disabled,false);
  else assert.equal(get('rollback-btn').disabled,true);
  if(path!=='/soul/proposals?status=pending')assert.equal(get('proposal-list').children.length,1);
  if(path!=='/soul/proposals?status=all')assert.equal(get('proposal-done-list').children.length,2);
});
