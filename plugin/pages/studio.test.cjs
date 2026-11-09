const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/studio.html','utf8');
function form(mode, checked, enabled=true){
  const nodes={prompt:{value:'小鲸鱼'},aspect:{value:'1:1'},'self-portrait':{checked:false},
    'follow-schedule':{checked:false},'visual-review':{checked},'start-painting':{},
    'asset-picker':{querySelectorAll:()=>[]}};
  const context={document:{querySelector:()=>({value:mode})},el:id=>nodes[id],busy:false,
    generationModes:enabled?['image','strokes']:['strokes']};
  vm.createContext(context);
  for(const name of ['request','updatePaintingMode']){
    vm.runInContext(html.match(new RegExp('^function '+name+'\\(\\).*$', 'm'))[0],context);
  }
  context.updatePaintingMode();
  return {body:context.request(),nodes};
}
test('both drawing modes carry explicit visual review choice into the request',()=>{
  for(const mode of ['image','strokes']){
    const on=form(mode,true),off=form(mode,false);
    assert.equal(on.body.mode,mode);
    assert.equal(on.body.visual_review,true);
    assert.equal(off.body.visual_review,false);
    assert.equal(on.nodes['visual-review'].disabled,false);
    assert.equal(on.nodes['start-painting'].disabled,false);
  }
});
test('unavailable image generation disables starting and its review option',()=>{
  const app=form('image',true,false);
  assert.equal(app.nodes['start-painting'].disabled,true);
  assert.equal(app.nodes['visual-review'].disabled,true);
});
async function paintingSequence(jobs){
  const nodes={};const el=id=>nodes[id]||(nodes[id]={textContent:'',hidden:false,children:[],appendChild(n){this.children.push(n);}});
  const context={el,viewingId:null,playing:false,pollTimer:null,clearTimeout:()=>{},
    call:async()=>jobs.shift(),loadPaintings:()=>{},status:message=>{throw Error(message);},
    clear:n=>{n.textContent='';n.children=[];},node:(tag,text)=>({tag,textContent:text}),
    paintingStates:{done:'已完成',failed:'失败'}};
  vm.createContext(context);
  vm.runInContext(html.match(/^function openPainting\(.*$/m)[0],context);
  return {nodes,el,open:context.openPainting};
}
test('painting detail displays saved parameters and never fills missing history with defaults',async()=>{
  assert.match(html,/id="painting-parameters"/);
  const workflow={sample:{inputs:{steps:17,seed:8123,cfg:1,sampler_name:'euler'}},latent:{inputs:{width:512,height:768}}};
  const jobs=[{id:'new',status:'failed',request:{mode:'image',prompt:'小鲸鱼',final_prompt:'最终要求'},
    generation:{prompt_id:'local-job',steps:17,seed:8123,workflow}},
    {id:'old',status:'failed',request:{mode:'image',prompt:'旧画'},generation:{prompt_id:'old-job',steps:25}},
    {id:'strokes',status:'failed',request:{mode:'strokes',prompt:'逐笔画'},model_turn_id:'plan-turn'},
    {id:'old-strokes',status:'failed',request:{mode:'strokes',prompt:'旧逐笔画'}}];
  const app=await paintingSequence(jobs);
  await app.open('new');
  let saved=JSON.parse(app.el('painting-parameters').textContent);
  assert.deepEqual(saved.generation.workflow,workflow);
  assert.equal(saved.request.final_prompt,'最终要求');
  assert.match(app.el('painting-parameters-note').textContent,/实际提交/);
  await app.open('old');
  saved=JSON.parse(app.el('painting-parameters').textContent);
  assert.equal(saved.generation.steps,25);
  assert.equal(saved.generation.workflow,undefined);
  assert.match(app.el('painting-parameters-note').textContent,/未保存完整/);
  assert.doesNotMatch(app.el('painting-parameters').textContent,/8123/);
  await app.open('strokes');
  saved=JSON.parse(app.el('painting-parameters').textContent);
  assert.equal(saved.generation,null);
  assert.match(app.el('painting-parameters-note').textContent,/逐笔/);
  await app.open('old-strokes');
  assert.match(app.el('painting-parameters-note').textContent,/旧记录未关联模型调用/);
});
