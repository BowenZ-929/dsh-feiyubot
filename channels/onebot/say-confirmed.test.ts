import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';

test('/say returns only acknowledged text, including replacement and partial replies', {timeout:20000}, async()=>{
  const temp=await mkdtemp(join(tmpdir(),'qqagent-say-confirmed-'));
  const reservation=createServer();reservation.listen(0,'127.0.0.1');await once(reservation,'listening');
  const port=(reservation.address() as {port:number}).port;
  await new Promise<void>(resolve=>reservation.close(()=>resolve()));
  const child=spawn(process.execPath,['server.ts','--port',String(port),'--core','http://127.0.0.1:1',
    '--state',join(temp,'state'),'--home',join(temp,'home'),'--prefs',join(temp,'prefs.jsonl')],
    {cwd:fileURLToPath(new URL('.',import.meta.url)),env:{...process.env,QQAGENT_CHANNEL_MODE:'send'},stdio:['ignore','pipe','pipe']});
  let logs='',socket:WebSocket|undefined,failAt=0;
  let frames:any[]=[];
  child.stdout.on('data',x=>{logs+=x;});child.stderr.on('data',x=>{logs+=x;});
  const wait=async(fn:()=>boolean)=>{const end=Date.now()+6000;while(!fn()&&Date.now()<end)await new Promise(r=>setTimeout(r,20));assert.ok(fn(),logs);};
  try{
    await wait(()=>logs.includes('反向 WS 已监听'));
    socket=new WebSocket(`ws://127.0.0.1:${port}/ws`);await once(socket,'open');
    socket.on('message',raw=>{
      const frame=JSON.parse(String(raw));frames.push(frame);
      const failed=frames.length===failAt;
      socket!.send(JSON.stringify({echo:frame.echo,status:failed?'failed':'ok',retcode:failed?1:0,data:{message_id:100+frames.length}}));
    });
    for(const scenario of [
      {name:'full',text:'第一句话。\n第二句话。',fail:0,expected:2},
      {name:'partial',text:'第一句话。\n第二句话。',fail:2,expected:1},
      {name:'failed',text:'第一句话。',fail:1,expected:0},
      {name:'replacement',text:'很长的一句话'.repeat(120),fail:0,expected:1},
    ]){
      frames=[];failAt=scenario.fail;
      const response=await fetch(`http://127.0.0.1:${port}/say`,{method:'POST',headers:{'content-type':'application/json'},
        body:JSON.stringify({scene:'private',target:100001,chunks:[{text:scenario.text}]})});
      assert.equal(response.status,200);
      const result:any=await response.json();
      assert.equal(result.sent,scenario.expected,scenario.name);
      const confirmed=frames.slice(0,result.sent).map(frame=>frame.params.message.filter((m:any)=>m.type==='text').map((m:any)=>m.data.text).join('')).join('\n');
      assert.equal(result.confirmed_text,confirmed,scenario.name);
      if(scenario.name==='replacement'){
        assert.equal(result.reason_code,'replaced_too_long');assert.notEqual(result.confirmed_text,scenario.text);
      }
      if(scenario.name==='partial')assert.ok(result.stopped);
    }
    frames=[];failAt=0;
    const media=await fetch(`http://127.0.0.1:${port}/say`,{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({scene:'private',target:100001,chunks:[{image:{file:'file:///tmp/fixture.png',summary:'测试图'}}]})});
    const mediaResult:any=await media.json();
    assert.equal(mediaResult.sent,1);assert.equal(mediaResult.confirmed_text,'');
    assert.equal(frames[0].params.message[0].type,'image');
    frames=[];
    const withMeme=await fetch(`http://127.0.0.1:${port}/say`,{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({scene:'private',target:100001,chunks:[
        {text:'给你留饭。\n坐这里。\n慢慢吃。'},
        {text:'开饭啦。',image:{file:'file:///tmp/rice.png',summary:'开饭啦'}},
      ]})});
    const withMemeResult:any=await withMeme.json();
    assert.equal(withMemeResult.reason_code,'sent');
    assert.equal(withMemeResult.sent,3);
    assert.equal(withMemeResult.confirmed_text,'给你留饭。\n坐这里。\n慢慢吃。\n开饭啦。');
    const imageSegments=frames.flatMap(f=>f.params.message).filter(m=>m.type==='image');
    assert.equal(imageSegments.length,1);
    assert.equal(imageSegments[0].data.file,'file:///tmp/rice.png');
    assert.equal(frames.at(-1).params.message.at(-1).type,'image');
    for (const scene of ['private', 'group']) {
      frames=[];
      const separate=await fetch(`http://127.0.0.1:${port}/say`,{method:'POST',headers:{'content-type':'application/json'},
        body:JSON.stringify({scene,target:scene==='group'?700000002:100001,chunks:[
          {text:'给你留饭。\n坐这里。\n慢慢吃。'},
          {text:'',image:{file:'file:///tmp/rice.png',summary:'开饭啦'}},
        ]})});
      const separated:any=await separate.json();
      assert.equal(separated.reason_code,'sent');
      assert.equal(separated.sent,3);
      assert.deepEqual(frames.map(f=>f.params.message.map((m:any)=>m.type)),[['text'],['text'],['image']]);
      assert.equal(frames[2].action,scene==='group'?'send_group_msg':'send_private_msg');
      assert.equal(frames[0].params.message[0].data.text,'给你留饭。');
      assert.equal(frames[1].params.message[0].data.text,'坐这里。\n慢慢吃。');
    }
  }finally{
    socket?.close();child.kill('SIGTERM');await once(child,'exit');
    await rm(temp,{recursive:true,force:true});
  }
});
