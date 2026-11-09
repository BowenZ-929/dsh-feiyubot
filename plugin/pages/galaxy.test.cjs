// Unit harness only: exercises data/list interactions, not a browser or visual QA.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(__dirname+'/galaxy.js','utf8');
class Element {
  constructor(){this.children=[];this.events={};this.dataset={};this.value='';this.textContent='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;}
  addEventListener(name,fn){this.events[name]=fn;}
  setAttribute(name,value){this[name]=value;}
  getContext(){return null;}
  getBoundingClientRect(){return {width:1000,height:600,left:0,top:0};}
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('a self-side placeholder is not displayed as a known recipient',async()=>{
  const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const memory={id:'event:self',kind:'event',label:'自言自语',quote:'我在呢',who:'她说',side:'她说',status:'active'};
  vm.runInNewContext(source,{document:{hidden:false,getElementById:get,createElement:()=>new Element(),addEventListener(){}},
    window:{location:{search:'?memory=event%3Aself',hash:''},history:{replaceState(){}},addEventListener(){}},URLSearchParams,Map,Set,Math,
    panelFetch:async(path)=>({ok:true,json:async()=>path.includes('/why')?{node:memory,edges:[],why:{note:''}}:
      {revision:'v1',nodes:[memory],edges:[],unresolved_edges:0}}),setTimeout(){},clearTimeout(){}});
  await settle();
  const walk=n=>[n,...n.children.flatMap(walk)],details=walk(get('detail'));
  const labels=details.map(n=>n.textContent),index=labels.indexOf('交流对象');
  assert.equal(labels[index+1],'未记录');
});
test('record-person navigation labels her reply recipient without inventing edge weight',async()=>{
  const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const memory={id:'event:m1',kind:'event',label:'她的回答',quote:'我在呢',who:'123456789',side:'她说',status:'active'};
  const person={id:'person:123456789',kind:'person',label:'联系人'};
  const edge={id:'record-person:event:m1',type:'record-person',from:person.id,to:memory.id,status:'active',basis:'memory.who'};
  const requests=[];
  vm.runInNewContext(source,{document:{hidden:false,getElementById:get,createElement:()=>new Element(),addEventListener(){}},
    window:{PANEL_BASE:'',location:{search:'?memory=event%3Am1',hash:''},history:{replaceState(){}},addEventListener(){}},URLSearchParams,Map,Set,Math,
    panelFetch:async(path)=>{requests.push(path);return {ok:true,json:async()=>path.includes('/why')?
      {node:path.includes('person%3A')?person:memory,edges:[edge],why:{note:'记录出处'}}:
      {revision:'v1',nodes:[memory,person],edges:[edge],unresolved_edges:0}};},setTimeout(){},clearTimeout(){}});
  await settle();
  const walk=n=>[n,...n.children.flatMap(walk)];
  const details=walk(get('detail')),content=details.map(n=>n.textContent).join('\n');
  assert.match(content,/交流对象/);assert.doesNotMatch(content,/说话人/);
  assert.match(content,/记录归属/);assert.doesNotMatch(content,/权重 undefined/);
  const button=details.find(n=>n.type==='button');await button.onclick();await settle();
  assert.ok(requests.includes('/api/memory/person%3A123456789/why'));
});
test('memory details link exact recorded turn under panel base, and never guess legacy turns',async()=>{
  for(const turn of ['651a77b183364c139e1278b34b65c0e0','x&source=other',undefined]){
    const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
    const memory={id:'event:m1',kind:'event',label:'原话',quote:'这张画很可爱',status:'active',turn_id:turn};
    vm.runInNewContext(source,{document:{hidden:false,getElementById:get,createElement:()=>new Element(),addEventListener(){}},
      window:{PANEL_BASE:'/plugins/qq',location:{search:'?memory=event%3Am1',hash:''},history:{replaceState(){}},addEventListener(){}},URLSearchParams,Map,Set,Math,
      panelFetch:async(path)=>({ok:true,json:async()=>path.includes('/why')?
        {node:memory,edges:[],why:{note:'累计记录'}}:{revision:'v1',nodes:[memory],edges:[],unresolved_edges:0}}),
      setTimeout(){},clearTimeout(){}});
    await settle();
    const walk=n=>[n,...n.children.flatMap(walk)],detail=walk(get('detail'));
    const links=detail.filter(n=>n.href);
    if(turn){
      assert.deepEqual(links.map(n=>n.href),['chat','model'].map(kind=>'/plugins/qq/observe?source='+kind+'&turn_id='+encodeURIComponent(turn)));
      assert.deepEqual(links.map(n=>n.textContent),['查看当时对话','查看当轮模型调用']);
    }else{
      assert.equal(links.length,0);
      assert.ok(detail.some(n=>n.textContent==='这条记忆没有记录聊天回合，无法直接追溯。'));
    }
  }
});
test('a linked retired memory opens its existing detail without restoring it',async()=>{
  const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('state').value='active';
  const memory={id:'event:old',kind:'event',label:'原计划',quote:'周六去公园',status:'stale'};
  const requests=[];
  vm.runInNewContext(source,{document:{hidden:false,getElementById:get,createElement:()=>new Element(),addEventListener(){}},
    window:{location:{search:'?memory=event%3Aold',hash:''},history:{replaceState(){}},addEventListener(){}},URLSearchParams,Map,Set,Math,
    panelFetch:async(path,options)=>{requests.push([path,options.method]);return {ok:true,json:async()=>path.includes('/why')?
      {node:memory,edges:[],why:{note:'当前原记录'}}:{revision:'v1',nodes:[memory],edges:[],unresolved_edges:0}};},
    setTimeout(){},clearTimeout(){}});
  await settle();
  assert.ok(requests.some(([path])=>path==='/api/memory/event%3Aold/why'));
  assert.equal(get('state').value,'all');
  assert.ok(get('detail').children.some(n=>n.textContent==='周六去公园'));
  assert.ok(requests.every(([,method])=>method==='GET'));
});
test('no-canvas fallback keeps real data searchable, details inert, and polling visibility-aware',async()=>{
  const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('state').value='active';
  const document={hidden:false,getElementById:get,createElement:()=>new Element(),events:{},addEventListener(n,f){this.events[n]=f;}};
  const memory={id:'event:m1',kind:'event',label:'<img src=x onerror=bad()>医院',text:'医院',quote:'原话',status:'active',source:'亲历'};
  const snapshot={revision:'v1',nodes:[memory,{id:'event:m2',kind:'event',label:'旧记忆',status:'stale'}],edges:[],unresolved_edges:0};
  let requests=[],nextTimer=null;
  vm.runInNewContext(source,{document,window:{location:{search:"",hash:""},history:{replaceState(){}},addEventListener(){}},URLSearchParams,Map,Set,Math,
    panelFetch:async(path,options)=>{requests.push([path,options.method]);return {ok:true,json:async()=>path.includes('/why')?
      {node:memory,edges:[],why:{note:'无排名记录'}}:snapshot};},
    setTimeout:fn=>{nextTimer=fn;return 1;},clearTimeout:()=>{nextTimer=null;}});
  await settle();
  assert.equal(get('space').hidden,true);
  assert.match(get('status').textContent,/先给你列表/);
  assert.equal(get('rows').children.length,1);
  const button=get('rows').children[0].children[0];
  assert.equal(button.textContent,memory.label); // user text is textContent, not markup
  await button.onclick();
  assert.equal(get('detail').children.some(n=>n.textContent==='原话'),true);
  assert.ok(requests.some(([path])=>path==='/api/memory/event%3Am1/why'));
  get('state').value='all';get('state').events.input();
  assert.equal(get('rows').children.length,2);
  get('search').value='不存在';get('search').events.input();
  assert.equal(get('rows').children.length,0);
  document.hidden=true;document.events.visibilitychange();
  assert.equal(nextTimer,null);
  const count=requests.length;
  await settle();assert.equal(requests.length,count);
  document.hidden=false;document.events.visibilitychange();await settle();
  assert.ok(requests.length>count);
  assert.ok(requests.every(([,method])=>method==='GET'));
});

for(const selected of ['', 'person:1'])test('large graph preserves anchors and readable labels: '+(selected||'unselected'),async()=>{
  const elements=new Map();const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  get('state').value='active';
  const painted=[],labels=[];
  const ctx={setTransform(){painted.length=0;labels.length=0;},setLineDash(){},beginPath(){},moveTo(){},lineTo(){},stroke(){},
    arc(){},closePath(){},fill(){painted.push(this.fillStyle);},fillText(value){labels.push(value);}};
  get('canvas').getContext=()=>ctx;
  const nodes=Array.from({length:608},(_,i)=>({id:'event:'+i,kind:'event',label:'记忆'+i,at:'2026-11-07',status:'active'}));
  nodes.push({id:'person:1',kind:'person',label:'某人'},{id:'topic:医院',kind:'topic',label:'医院'});
  const edges=nodes.filter(n=>n.kind==='event').map(n=>({from:'person:1',to:n.id,type:'record-person',status:'active'}));
  vm.runInNewContext(source,{document:{hidden:false,getElementById:get,createElement:()=>new Element(),addEventListener(){}},
    window:{location:{search:selected?'?memory='+encodeURIComponent(selected):'',hash:""},history:{replaceState(){}},addEventListener(){},devicePixelRatio:1},URLSearchParams,Map,Set,Math,
    panelFetch:async(path)=>({ok:true,json:async()=>path.includes('/why')?{node:nodes[608],edges,why:{note:''}}:{revision:'v1',nodes,edges,unresolved_edges:0}}),
    setTimeout(){},clearTimeout(){}});
  await settle();
  assert.equal(painted.length,500);
  assert.ok(painted.includes('#8fc7ff'),'person anchor must not be pushed out by newer memories');
  assert.ok(painted.includes('#c7afff'),'topic anchor must not be pushed out by newer memories');
  if(selected)assert.deepEqual(labels,['某人'],'memory labels belong in the sidebar, not piled over the canvas');
});
