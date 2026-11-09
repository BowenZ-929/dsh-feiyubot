const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/about.html','utf8');
const source=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function setup(fail=false,search=""){
 const elements=new Map(),requests=[];
 class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.events={};this.value='';this.text='';}
  set textContent(v){this.text=v;this.children=[];}get textContent(){return this.text+this.children.map(n=>n.textContent).join('');}
  get firstChild(){return this.children[0];}appendChild(n){this.children.push(n);return n;}
  removeChild(n){this.children.splice(this.children.indexOf(n),1);}addEventListener(e,f){this.events[e]=f;}
 }
 for(const m of html.matchAll(/id="([^"]+)"/g))elements.set(m[1],new Element());
 const item={id:'p1',session_id:'qq:private:100001',text:'少说套话<script>',evidence:['以后和我聊天别端着'],at:'2026-11-09T08:00:00+09:00',entry_ids:['m1'],replaces:[],actor:'conversation'};
 const history=[item];let current=[item];
 vm.runInNewContext(source,{window:{location:{search}},URLSearchParams,document:{getElementById:id=>elements.get(id),createElement:t=>new Element(t)},
 panelFetch:async(path,init)=>{
  requests.push({path,...init});
  let data;
  if(path==='/brain/people')data={people:[{id:'100001',display:'虚构朋友',memory_count:1},{id:'100002',display:'另一位朋友',memory_count:0}]};
  else if(path.endsWith('/card'))data={history:[]};
  else if(path.endsWith('/preferences')){
   if(fail)return {status:503,json:async()=>({message:'读取失败'})};
   if(init.method==='POST'){assert.deepEqual(JSON.parse(init.body),{id:'p1'});current=[];history.push({...item,id:'p2',text:'',evidence:[],replaces:['p1'],actor:'panel'});data={ok:true};}
   else data={ok:true,current,history};
  }else throw Error(path);
  return {status:200,json:async()=>data};
 }});
 return {get:id=>elements.get(id),requests};
}
test('shows scope and literal evidence, retracts only by deliberate button, retains history',async()=>{
 const app=setup();await settle();
 const list=app.get('preference-list');assert.ok(list,'preference section exists');
 assert.match(list.textContent,/少说套话<script>/);assert.match(list.textContent,/以后和我聊天别端着/);
 assert.match(list.textContent,/qq:private:100001/);
 assert.ok(app.requests.every(r=>r.method==='GET'));
 const button=list.children[0].children.find(n=>n.tagName==='BUTTON');assert.ok(button);
 await button.events.click();await settle();
 assert.match(list.textContent,/还没有/);assert.match(app.get('preference-history').textContent,/撤回/);
 assert.equal(app.requests.filter(r=>r.method==='POST').length,1);
});
test('preference read failure is visible',async()=>{
 const app=setup(true);await settle();assert.match(app.get('preference-msg').textContent,/读取失败/);
});

test('person deep link selects only an existing person',async()=>{
 const target=setup(false,'?who=100002');await settle();
 assert.ok(target.requests.some(r=>r.path==='/brain/people/100002/preferences'));
 assert.equal(target.get('person-name').textContent,'另一位朋友');
 const missing=setup(false,'?who=not-here');await settle();
 assert.equal(missing.get('person-name').textContent,'虚构朋友');
 assert.ok(!missing.requests.some(r=>r.path.includes('not-here')));
});
