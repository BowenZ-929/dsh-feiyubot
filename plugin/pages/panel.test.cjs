// Actual shell script; browser checks cover joint iframe history separately.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const html=fs.readFileSync(__dirname+'/panel.html','utf8');
const source=html.match(/<script>\s*([\s\S]*?)<\/script>/)[1];

function setup(base='',hash=''){
  let url=new URL('http://local'+base+'/panel'+hash);
  const events={},frameEvents={},navigated=[];
  const links=[...html.matchAll(/<a href="([^"]+)"[^>]*>([^<]+)<\/a>/g)].map(([,href,text])=>({
    href:new URL(href,url).href,textContent:text,className:'',attrs:{href},
    getAttribute(name){return this.attrs[name];},setAttribute(name,value){this.attrs[name]=value;},
    removeAttribute(name){delete this.attrs[name];}
  }));
  const frame={contentDocument:{URL:new URL('./',url).href,title:'魂'},
    contentWindow:{addEventListener(name,fn){frameEvents[name]=fn;},location:{replace(href){navigated.push(href);frame.contentDocument={URL:new URL(href,url).href,title:'测试页'};}}},
    addEventListener(name,fn){frameEvents[name]=fn;}};
  const tabs={querySelectorAll:()=>links,addEventListener(){}};
  const context={URL,document:{getElementById:id=>id==='tabs'?tabs:null,querySelector:()=>frame,title:'她的面板',currentScript:{getAttribute:()=> 'panel'}},
    window:{addEventListener(name,fn){events[name]=fn;}},
    location:url,history:{replaceState(_s,_t,href){url=new URL(href,url);context.location=url;}}};
  vm.runInNewContext(fs.readFileSync(__dirname+'/panel-base.js','utf8'),context);
  assert.equal(context.window.PANEL_BASE,base);
  vm.runInNewContext(source,context);
  return {links,frame,navigated,hash:()=>url.hash,
    load(path){frame.contentDocument={URL:'http://local'+base+path,title:'测试页'};frameEvents.load?.();},
    routechange(path){frame.contentDocument.URL='http://local'+base+path;frameEvents['panel-routechange']?.();},
    hashchange(hash){url.hash=hash;context.location=url;events.hashchange?.();},
    active:()=>links.filter(l=>l.attrs['aria-current']==='page').map(l=>l.textContent)};
}

for(const base of ['', '/qqagent/panel']) test('restores page with query and selected memory under '+(base||'core'),()=>{
  const app=setup(base,'#/galaxy?memory=event%3At-example#detail');
  assert.equal(app.navigated.at(-1),'http://local'+base+'/galaxy?memory=event%3At-example#detail');
  app.load('/galaxy?memory=event%3At-example#detail');
  assert.deepEqual(app.active(),['记忆']);
  assert.equal(app.hash(),'#/galaxy?memory=event%3At-example#detail');
});

test('child navigation updates the selected tab and URL without loading another frame',()=>{
  const app=setup();app.load('/day');assert.deepEqual(app.active(),['今天']);
  app.load('/observe?source=chat');
  assert.deepEqual(app.active(),['观察']);assert.equal(app.hash(),'#/observe?source=chat');
  assert.equal(app.navigated.length,0);
  app.load('/phone');assert.deepEqual(app.active(),['设置']);
});

test('changing the outer address restores the child without duplicating same-page loads',()=>{
  const app=setup();app.load('/day');app.hashchange('#/observe?source=chat');
  assert.equal(app.navigated.length,1);
  app.load('/observe?source=chat');app.hashchange('#/observe?source=chat');
  assert.equal(app.navigated.length,1);
});

test('in-page record filters sync shell address without reloading the iframe',()=>{
  const app=setup('/qqagent/panel');app.load('/observe?source=chat');
  app.routechange('/observe?source=chat&offset=20&q=test');
  assert.equal(app.hash(),'#/observe?source=chat&offset=20&q=test');
  assert.deepEqual(app.active(),['观察']);assert.equal(app.navigated.length,0);
});

for(const hash of ['#//outside.test/day','#/missing','#/panel','#/../private']) test('does not load unsupported target '+hash,()=>{
  const app=setup('',hash);
  assert.equal(app.navigated.length,0);
  app.load('/');
  assert.deepEqual(app.active(),['魂']);
  assert.equal(app.hash(),'#/');
});
