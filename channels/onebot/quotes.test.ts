import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { renderQuote, resolveQuote, quoteFromMsgData, type QuoteContent } from "./quotes.ts";
import { InboundRing } from "./inbound-ids.ts";

const own: QuoteContent={text:"我的话",who:"99",speaker:"机器人自己"};
const other: QuoteContent={text:"原话",who:"42",speaker:"同名(42)"};

test("引用依次查自己、入站和补查，保留原作者而非当前发言者", async () => {
  const deps={own:(id:number)=>id===1?own:null,inbound:(id:number)=>id<=2?other:null,
    getMsg:async(id:number)=>id<=3?{...other,who:"43",speaker:"同名(43)"}:null};
  for(const [id,source,who] of [[1,"own","99"],[2,"inbound","42"],[3,"get_msg","43"],[4,"none",""]] as const){
    const result=await resolveQuote(id,deps);
    assert.equal(result.source,source);assert.equal(result.who,who);assert.equal(result.message_id,String(id));
  }
  assert.match(renderQuote(await resolveQuote(1,deps)),/机器人自己/);
  assert.match(renderQuote(await resolveQuote(4,deps)),/作者未记录.*内容读不到/);
});

test("补查保留多段原文与媒体顺序，作者只信发送者字段",()=>{
  const data={sender:{user_id:42,nickname:"旧昵称",card:"群名片"},message:[
    {type:"text",data:{text:" 不是周六，"}},{type:"image",data:{file:"image"}},
    {type:"text",data:{text:"是周日。 "}}]};
  assert.deepEqual(quoteFromMsgData(data),{text:" 不是周六，[图片]是周日。 ",who:"42",speaker:"群名片(42)"});
  assert.deepEqual(quoteFromMsgData({message:data.message}),{text:" 不是周六，[图片]是周日。 ",who:"",speaker:""});
  assert.equal(quoteFromMsgData({message:[]}),null);
});

test("引用显示保留空格，不把message_id当成作者或正文",()=>{
  const quote={...other,text:"  很长的话！  ",message_id:"1234567",source:"inbound" as const};
  assert.equal(renderQuote(quote),"（引用同名(42)的消息：「  很长的话！  」）");
  assert.ok(!renderQuote(quote).includes("1234567"));
});

test("入站环重载保留名片与身份，旧条目无名片不冒充当前人",()=>{
  const dir=mkdtempSync(join(tmpdir(),"quote-ring-"));
  try{
    const path=join(dir,"inbound.json");
    writeFileSync(path,JSON.stringify({entries:[{id:1,who:42,text:"旧原话",at:""}]}));
    const ring=new InboundRing({path});ring.add({id:2,who:43,name:"同名",text:"另一人的话",at:""});
    const loaded=new InboundRing({path});
    assert.deepEqual(loaded.quoteOf(1),{who:"42",speaker:"42",text:"旧原话"});
    assert.deepEqual(loaded.quoteOf(2),{who:"43",speaker:"同名(43)",text:"另一人的话"});
    assert.equal(loaded.textOf(2),"另一人的话");
  }finally{rmSync(dir,{recursive:true,force:true});}
});

test("未取到原文时保留已知自己身份，失败仍交给调用方处理",async()=>{
  const deps={own:()=>({...own,text:null}),inbound:()=>null,getMsg:async()=>null};
  assert.match(renderQuote(await resolveQuote(9,deps)),/机器人自己.*内容读不到/);
  await assert.rejects(()=>resolveQuote(9,{...deps,getMsg:async()=>{throw new Error("boom");}}));
});

test("旧own只有编号时，补查正文不丢失已确认的机器人作者",async()=>{
  const result=await resolveQuote(1,{own:()=>({...own,text:null}),inbound:()=>null,
    getMsg:async()=>({text:"补查的原文",who:"",speaker:""})});
  assert.equal(result.who,"99");assert.equal(result.speaker,"机器人自己");
  assert.equal(result.text,"补查的原文");assert.equal(result.source,"get_msg");
});

test("引用里的QQ表情名称与直接消息一致，作者仍是原发送者", () => {
  assert.deepEqual(quoteFromMsgData({ sender: { user_id: 42, nickname: "小周" }, message: [
    { type: "text", data: { text: "厉害" } },
    { type: "face", data: { id: "76", raw: { faceText: null } } },
  ] }), { text: "厉害[表情:赞]", who: "42", speaker: "小周(42)" });
});
