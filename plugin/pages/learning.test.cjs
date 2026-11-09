const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/learning.html','utf8');
const script=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];
const settle=()=>new Promise(resolve=>setImmediate(resolve));
class Element {
  constructor(tag){this.tagName=tag;this.children=[];this.textContent='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;this.textContent='';}
}
const text=node=>[node.textContent,...node.children.map(text)].join('\n');
for(const base of ['', '/qqagent/panel'])test('learning distinguishes partial and replaced notices in current and historical views: '+base,async()=>{
  const nodes=new Map([...html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g)].map(m=>[m[2],new Element(m[1])]));
  const notice={status:'partial',text:'<img src=x> 原拟发送通知'};
  const job={id:'a'.repeat(32),status:'done',cards:[],destination:{who:'测试者',scene:'group',target:7},
    delivery:notice,deliveries:[{status:'replaced',text:'之前的长通知'},notice]};
  const requests=[];
  const context={URL,location:new URL('http://local'+base+'/learning'),window:{addEventListener(){}},setTimeout(){},clearTimeout(){},
    document:{getElementById:id=>nodes.get(id),createElement:tag=>new Element(tag),addEventListener(){},currentScript:{getAttribute:()=> 'learning'}},
    fetch:async(path,init)=>{requests.push({path,init});return {ok:true,json:async()=>path.includes('/learning/read?')?job:{jobs:[{id:job.id,title:'虚构资料',status:'done',cards:0}]}};}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/panel-base.js','utf8'),context);
  context.panelFetch=context.window.panelFetch;
  vm.runInNewContext(script,context);await settle();
  await nodes.get('jobs').children[0].onclick();
  const rendered=text(nodes.get('detail'));
  assert.match(rendered,/仅部分气泡确认发出，不自动重发/);
  assert.match(rendered,/通道改发了简短提示，原通知未完整发送/);
  assert.match(rendered,/查看拟发送通知（不代表实际送达原文）/);
  assert.match(rendered,/<img src=x> 原拟发送通知/);
  assert.doesNotMatch(rendered,/通道已确认发出/);
  assert.ok(requests.every(r=>r.init.method==='GET'&&r.path.startsWith(base+'/learning/')));
});
