import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,readFile,writeFile,readdir,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';

for (const withCore of [true,false]) test(`runtime reports loaded settings, read-only; core=${withCore}`,{timeout:20000},async()=>{
  const temp=await mkdtemp(join(tmpdir(),'qqagent-runtime-'));
  const gatePath=join(temp,'gate.json');
  const words=JSON.parse(await readFile(new URL('./data/gate.json',import.meta.url),'utf8'));
  await writeFile(gatePath,JSON.stringify(words));
  const probe=createServer();probe.listen(0,'127.0.0.1');await once(probe,'listening');
  const port=(probe.address() as any).port;await new Promise<void>(r=>probe.close(()=>r()));
  const args=['server.ts','--port',String(port),'--state',join(temp,'state'),'--home',join(temp,'home'),
    '--gate',gatePath,'--core-timeout','12345'];
  if(withCore)args.push('--core','http://127.0.0.1:9');
  const child=spawn(process.execPath,args,{cwd:new URL('.',import.meta.url),
    env:{...process.env,QQAGENT_CHANNEL_MODE:'silent',RUNTIME_TEST_SECRET:'never-return-this'},stdio:['ignore','pipe','pipe']});
  let logs='';child.stdout.on('data',d=>logs+=d);child.stderr.on('data',d=>logs+=d);
  try{
    const end=Date.now()+10000;
    while(!logs.includes('监听')&&Date.now()<end)await new Promise(r=>setTimeout(r,20));
    assert.ok(logs.includes('监听'),logs);
    const response=await fetch(`http://127.0.0.1:${port}/runtime`);
    assert.equal(response.status,200);
    const d=await response.json() as any;
    assert.equal(d.channel,'onebot-business');assert.equal(d.mode,'silent');
    assert.equal(d.core_enabled,withCore);assert.equal(d.core_timeout_ms,12345);
    assert.ok(Number.isFinite(Date.parse(d.started_at)));
    assert.doesNotMatch(JSON.stringify(d),/never-return-this|qqagent-runtime-|127\.0\.0\.1:9/);
    if(withCore){
      assert.equal(d.gate_words.chitchatSampleMod,words.chitchatSampleMod);
      assert.equal(d.policy.batch.openMs.privateOpen,3500);
      assert.equal(d.disk_matches,true);
      await writeFile(gatePath,JSON.stringify({...words,chitchatSampleMod:words.chitchatSampleMod+1}));
      const changed=await (await fetch(`http://127.0.0.1:${port}/runtime`)).json() as any;
      assert.equal(changed.disk_matches,false);
      assert.equal(changed.gate_words.chitchatSampleMod,words.chitchatSampleMod);
      await writeFile(gatePath,'not json');
      const broken=await (await fetch(`http://127.0.0.1:${port}/runtime`)).json() as any;
      assert.equal(broken.disk_matches,null);assert.ok(broken.config_error);
      assert.equal(broken.gate_words.chitchatSampleMod,words.chitchatSampleMod);
      assert.deepEqual(await readdir(join(temp,'state')),[],'read-only requests do not create ledgers');
    }else{
      assert.equal(d.policy,null);assert.equal(d.gate_words,null);assert.equal(d.disk_matches,null);
      assert.deepEqual(await readdir(temp),['gate.json']);
    }
    assert.equal((await fetch(`http://127.0.0.1:${port}/runtime`,{method:'POST'})).status,404);
  }finally{
    if(child.exitCode===null){child.kill('SIGTERM');await once(child,'exit');}
    await rm(temp,{recursive:true,force:true});
  }
});
