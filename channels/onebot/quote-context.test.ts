import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import WebSocket from 'ws';

test('真实通道把引用原作者和完整上下文独立传核，未把引用作为当前正文', {timeout:15000}, async()=>{
  const temp=await mkdtemp(join(tmpdir(),'qqagent-quote-context-'));
  const state=join(temp,'state');await mkdir(state);
  await writeFile(join(state,'own.json'),JSON.stringify({entries:[{id:801,text:'机器人原话',at:''}]}));
  const calls:any[]=[],queries:number[]=[];
  const core=createServer(async(req,res)=>{
    let raw='';for await(const part of req)raw+=part;
    calls.push(JSON.parse(raw));res.setHeader('content-type','application/json');res.end('{"ok":true,"chunks":[]}');
  });
  core.listen(0,'127.0.0.1');await once(core,'listening');
  const probe=createServer();probe.listen(0,'127.0.0.1');await once(probe,'listening');
  const port=(probe.address() as {port:number}).port;await new Promise<void>(resolve=>probe.close(()=>resolve()));
  const child=spawn(process.execPath,['server.ts','--port',String(port),'--core',`http://127.0.0.1:${(core.address() as {port:number}).port}`,
    '--state',state,'--home',join(temp,'home'),'--prefs',join(temp,'prefs.jsonl')],
    {cwd:fileURLToPath(new URL('.',import.meta.url)),env:{...process.env,QQAGENT_CHANNEL_MODE:'send'},stdio:['ignore','pipe','pipe']});
  let logs='',socket:WebSocket|undefined;
  child.stdout.on('data',x=>{logs+=x;});child.stderr.on('data',x=>{logs+=x;});
  const wait=async(fn:()=>boolean)=>{const end=Date.now()+6000;while(!fn()&&Date.now()<end)await new Promise(r=>setTimeout(r,20));assert.ok(fn(),logs);};
  try{
    await wait(()=>logs.includes('反向 WS 已监听'));
    socket=new WebSocket(`ws://127.0.0.1:${port}/ws`);await once(socket,'open');
    socket.on('message',raw=>{
      const frame=JSON.parse(String(raw));assert.equal(frame.action,'get_msg','测试核心沉默，不应生成发送动作');
      queries.push(frame.params.message_id);
      socket!.send(JSON.stringify({echo:frame.echo,status:frame.params.message_id===702?'failed':'ok',retcode:frame.params.message_id===702?1:0,
        data:{sender:{user_id:410,card:'同名'},message:[{type:'text',data:{text:'不是周六，'}},{type:'image',data:{file:'example'}},{type:'text',data:{text:'是周日。'}}]}}));
    });
    let id=900;
    const say=(group:number,who:number,message:any[],messageId=++id)=>socket!.send(JSON.stringify({post_type:'message',message_type:'group',
      self_id:999,group_id:group,user_id:who,sender:{user_id:who,card:'同名'},message_id:messageId,message}));
    say(71,410,[{type:'text',data:{text:'给我画猫'}}],700);
    await wait(()=>logs.includes('key=mid:700 → b1'));
    for(const [group,replyTo] of [[71,700],[72,701],[73,801],[74,702]]){
      say(group,420,[{type:'reply',data:{id:String(replyTo)}},{type:'at',data:{qq:'999'}},{type:'text',data:{text:'这是引用，不是我的要求'}}]);
    }
    await wait(()=>calls.length===4);
    const a=calls.find(c=>c.where.groupId===71), b=calls.find(c=>c.where.groupId===72), self=calls.find(c=>c.where.groupId===73), missing=calls.find(c=>c.where.groupId===74);
    assert.equal(a.segment.at(-1).who,'420');assert.equal(a.segment.at(-1).quote.who,'410');
    assert.equal(a.segment.at(-1).quote.text,'给我画猫');assert.ok(!a.segment.at(-1).text.includes('给我画猫'));
    assert.equal(b.segment[0].quote.text,'不是周六，[图片]是周日。');assert.equal(b.segment[0].quote.who,'410');
    assert.equal(self.segment[0].quote.who,'999');assert.equal(self.segment[0].quote.speaker,'机器人自己');
    assert.equal(missing.segment[0].quote.source,'none');assert.equal(missing.segment[0].quote.who,'');
    assert.deepEqual(queries.sort(),[701,702]);
  }finally{
    socket?.terminate();child.kill('SIGTERM');await once(child,'exit');
    core.closeAllConnections();await new Promise<void>(resolve=>core.close(()=>resolve()));
    await rm(temp,{recursive:true,force:true});
  }
});
