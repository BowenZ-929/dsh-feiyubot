import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {mkdtemp,rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import WebSocket from 'ws';

test('睡眠提示单泡原样发送，实际回执可准确去重', {timeout:25000}, async()=>{
  const temp=await mkdtemp(join(tmpdir(),'qqagent-sleep-notice-'));
  const notice='晚安啦，明天见';
  let receipt:any, logs='', socket:WebSocket|undefined;
  const sent:any[]=[];
  const core=createServer(async(req,res)=>{
    let text='';for await(const chunk of req)text+=chunk;
    const body=JSON.parse(text);res.setHeader('content-type','application/json');
    if(req.url==='/chat/ack'){receipt=body;res.end('{"ok":true}');return;}
    res.end(JSON.stringify({ok:true,send:true,reply_kind:'sleep_notice',delivery_id:'notice',
      chunks:[{text:notice,gap_before:0,send_at:0}]}));
  });
  core.listen(0,'127.0.0.1');await once(core,'listening');
  const probe=createServer();probe.listen(0,'127.0.0.1');await once(probe,'listening');
  const port=(probe.address() as any).port;await new Promise<void>(r=>probe.close(()=>r()));
  const child=spawn(process.execPath,['server.ts','--port',String(port),'--core',`http://127.0.0.1:${(core.address() as any).port}`,
    '--state',join(temp,'state'),'--home',join(temp,'home'),'--prefs',join(temp,'prefs.jsonl')],
    {cwd:new URL('.',import.meta.url),env:{...process.env,QQAGENT_CHANNEL_MODE:'send'},stdio:['ignore','pipe','pipe']});
  child.stdout.on('data',d=>logs+=d);child.stderr.on('data',d=>logs+=d);
  const wait=async(fn:()=>boolean)=>{const end=Date.now()+16000;while(!fn()&&Date.now()<end)await new Promise(r=>setTimeout(r,20));assert.ok(fn(),logs);};
  try{
    await wait(()=>logs.includes('监听')&&logs.includes(String(port)));
    socket=new WebSocket(`ws://127.0.0.1:${port}/ws`);await once(socket,'open');
    socket.on('message',data=>{const frame=JSON.parse(String(data));if(frame.action?.startsWith('send_')){
      sent.push(frame);socket!.send(JSON.stringify({status:'ok',retcode:0,echo:frame.echo,data:{message_id:102}}));
    }});
    socket.send(JSON.stringify({post_type:'message',message_type:'private',sub_type:'friend',self_id:999,user_id:42,
      sender:{user_id:42,nickname:'虚构用户'},time:Math.floor(Date.now()/1000),message_id:101,
      message:[{type:'text',data:{text:'在吗'}}]}));
    await wait(()=>!!receipt);
    assert.equal(sent.length,1);
    assert.equal(receipt.text,notice);
    assert.equal(receipt.delivery_id,'notice');
    for(const body of [
      {scene:'group',target:42,reply_kind:'sleep_notice',chunks:[{text:notice}]},
      {scene:'private',target:42,reply_kind:'unknown',chunks:[{text:notice}]},
      {scene:'private',target:42,reply_kind:'sleep_notice',chunks:[{text:'长'.repeat(61)}]},
      {scene:'private',target:42,reply_kind:'sleep_notice',chunks:[{text:notice},{text:'再一句'}]},
    ]){
      const response=await fetch(`http://127.0.0.1:${port}/say`,{method:'POST',
        headers:{'content-type':'application/json'},body:JSON.stringify(body)});
      assert.equal(response.status,400);
    }
    assert.equal(sent.length,1,'无效通知不能发送');
    const response=await fetch(`http://127.0.0.1:${port}/say`,{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify({scene:'private',target:42,
        reply_kind:'sleep_notice',chunks:[{text:notice}]})});
    const timer=await response.json() as any;
    assert.equal(timer.sent,1);
    assert.equal(timer.confirmed_text,notice);
    assert.equal(sent.length,2,'后台告别同样只发一泡');
    const emojiNotice='睡'.repeat(59)+'😴';
    const emojiResponse=await fetch(`http://127.0.0.1:${port}/say`,{method:'POST',
      headers:{'content-type':'application/json'},body:JSON.stringify({scene:'private',target:43,
        reply_kind:'sleep_notice',chunks:[{text:emojiNotice}]})});
    assert.equal(emojiResponse.status,200,'60字含emoji与成长字段采用相同字数口径');
    assert.equal((await emojiResponse.json() as any).confirmed_text,emojiNotice);
  }finally{
    socket?.terminate();child.kill('SIGTERM');await once(child,'exit');core.closeAllConnections();
    await new Promise<void>(r=>core.close(()=>r()));await rm(temp,{recursive:true,force:true});
  }
});
