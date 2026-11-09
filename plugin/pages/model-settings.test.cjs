const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/settings.html','utf8');
const file=__dirname+'/model-settings.js';
const source=fs.existsSync(file)?fs.readFileSync(file,'utf8'):'';
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function setup(){
 const nodes=new Map([...html.matchAll(/id="([^"]+)"/g)].map(m=>[m[1],{value:'',textContent:'',disabled:true,events:{},addEventListener(k,f){this.events[k]=f;}}]));
 const state={fail:false,config:{model:'deepseek-flash',thinking:{type:'enabled'},reasoning_effort:'high',max_tokens:8000,timeout_ms:30000,retries:0,temperature:.7}},requests=[];
 vm.runInNewContext(source,{document:{getElementById:id=>nodes.get(id)},panelFetch:async(path,init)=>{
  requests.push({path,method:init.method,body:init.body&&JSON.parse(init.body)});
  return {status:state.fail?503:200,json:async()=>state.fail?{message:'未保存'}:{ok:true,config:state.config,temperature_tiers:{factual:.5},limits:{max_tokens:393216},source:'factory'}};
 }});
 return {get:id=>nodes.get(id),state,requests};
}
test('reads model values without writing and keeps the form disabled on failure',async()=>{
 const a=setup();await settle();assert.ok(a.get('model-form'));
 assert.equal(a.get('model-name').value,'deepseek-flash');
 assert.equal(a.get('model-effort').value,'high');
 assert.equal(a.get('model-save').disabled,false);
 assert.equal(a.get('model-tokens').max,393216);
 assert.deepEqual(a.requests,[{path:'/model/status',method:'GET',body:undefined}]);
 a.state.fail=true;await a.get('model-refresh').events.click();
 assert.equal(a.get('model-save').disabled,true);assert.match(a.get('model-status').textContent,/未保存/);
});
test('saves only public request parameters and reports a failed save honestly',async()=>{
 const a=setup();await settle();assert.ok(a.get('model-form'));
 a.get('model-effort').value='low';
 await a.get('model-form').events.submit({preventDefault(){}});
 const sent=a.requests[1];assert.equal(sent.path,'/model/config');assert.equal(sent.method,'POST');
 assert.equal(sent.body.reasoning_effort,'low');assert.equal(sent.body.max_tokens,8000);
 assert.equal(sent.body.thinking.type,'enabled');assert.ok(!('key_env' in sent.body));
 a.state.fail=true;await a.get('model-form').events.submit({preventDefault(){}});
 assert.match(a.get('model-status').textContent,/没有保存/);
 assert.match(html,/温度在思考模式下不生效/);
});
