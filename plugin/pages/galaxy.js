/* Layout/projection adapted from dsh-Memory-Galaxy (MIT), Derpyu520 & 大肥鱼.
 * See galaxy-REFERENCE-LICENSE.txt. Read-only view; no imported memory/forgetting model.
 */
(function () {
  "use strict";
  const el = id => document.getElementById(id);
  const names = {person:"人物",event:"记忆",topic:"话题",active:"在役",stale:"已退役",archived:"冷库",
    today:"今天",hot:"近期",longterm:"长期",cold:"冷库",vivid:"清晰",clear:"清楚",fuzzy:"模糊",trace:"轮廓"};
  const colors = {person:"#8fc7ff",event:"#f1c28c",topic:"#c7afff"};
  const canvas = el("canvas");
  let ctx;
  try { ctx = canvas.getContext("2d"); } catch (_) { ctx = null; }
  let snapshot = null, byId = new Map(), visible = [], plotted = [], selected = new URLSearchParams(window.location.search).get("memory") || "", limit = 50;
  if (selected) el("state").value = "all";
  let camera = {yaw:.4,pitch:.85,dist:1500}, dragging = null, timer = null, pending = false;
  let listOnly = !ctx;
  function node(tag, text, cls) {
    const n = document.createElement(tag); n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function position(id, kind) {
    // Stable ID seed: adding a memory does not move every other star.
    let hash = 2166136261;
    for (const c of id) hash = Math.imul(hash ^ c.charCodeAt(0), 16777619) >>> 0;
    const rnd = () => {hash = (Math.imul(hash,1664525)+1013904223)>>>0; return hash/4294967296;};
    const rr = (kind === "person" ? .12 : .3) + rnd() * (kind === "person" ? .45 : .65);
    const theta = Math.floor(rnd()*4)/4*Math.PI*2 - 2.6*Math.log(rr*6+1) + (rnd()-.5)*1.3;
    const radius = 1000*rr;
    return {x:Math.cos(theta)*radius,z:Math.sin(theta)*radius,y:(rnd()-.5)*110};
  }
  function project(p, w, h) {
    const cy=Math.cos(camera.yaw), sy=Math.sin(camera.yaw), cp=Math.cos(camera.pitch), sp=Math.sin(camera.pitch);
    const x=p.x*cy-p.z*sy, zz=p.x*sy+p.z*cy, y=p.y*cp-zz*sp, z=p.y*sp+zz*cp;
    const s=(Math.min(w,h)/2300)*(1500/camera.dist)*(2400/Math.max(500,2400+z));
    return {x:w/2+x*s,y:h*.46+y*s,z,s};
  }
  function draw() {
    if (!ctx || listOnly || document.hidden) return;
    const rect=canvas.getBoundingClientRect(), w=rect.width, h=rect.height, dpr=Math.min(2,window.devicePixelRatio||1);
    canvas.width=Math.round(w*dpr); canvas.height=Math.round(h*dpr); ctx.setTransform(dpr,0,0,dpr,0,0);
    if(visible.length<3){plotted=[];return;}
    // Preserve relationship anchors at the canvas budget; a selected old memory must stay visible too.
    const focus=new Set(selected?[selected]:[]);
    for(const edge of snapshot?.edges||[]){
      if(selected&&(edge.from===selected||edge.to===selected)){focus.add(edge.from);focus.add(edge.to);}
    }
    const priority=n=>n.id===selected?0:n.kind!=="event"?1:focus.has(n.id)?2:3;
    plotted=visible.slice().sort((a,b)=>priority(a)-priority(b)).slice(0,500)
      .map(n=>({...project(position(n.id,n.kind),w,h),node:n})).sort((a,b)=>b.z-a.z);
    const points=new Map(plotted.map(p=>[p.node.id,p]));
    const neighbors=new Set();
    for (const edge of snapshot?.edges || []) {
      if (!selected || (edge.from!==selected && edge.to!==selected)) continue;
      if (el("state").value!=="all" && edge.status!=="active") continue;
      const a=points.get(edge.from), b=points.get(edge.to);
      if (!a || !b) continue;
      neighbors.add(edge.from); neighbors.add(edge.to);
      ctx.strokeStyle=edge.status==="active"?"#9bb8df88":"#9bb8df44";
      ctx.setLineDash(edge.status==="active"?[]:[3,5]);ctx.beginPath();ctx.moveTo(a.x,a.y);ctx.lineTo(b.x,b.y);ctx.stroke();
    }
    ctx.setLineDash([]);
    for (const p of plotted) {
      const n=p.node, chosen=n.id===selected, related=neighbors.has(n.id);
      const radius=chosen?6:n.kind==="event"?3:4;
      ctx.globalAlpha=(selected && !chosen && !related) ? .3 : 1;
      ctx.fillStyle=colors[n.kind];ctx.strokeStyle=colors[n.kind];
      ctx.shadowBlur=chosen?18:8;ctx.shadowColor=colors[n.kind];
      ctx.beginPath();
      if(n.kind==="topic"){ctx.moveTo(p.x,p.y-radius-1);ctx.lineTo(p.x+radius+1,p.y);ctx.lineTo(p.x,p.y+radius+1);ctx.lineTo(p.x-radius-1,p.y);ctx.closePath();}
      else ctx.arc(p.x,p.y,radius,0,Math.PI*2);
      if(n.status && n.status!=="active")ctx.stroke();else ctx.fill();
      ctx.shadowBlur=0;
      if(chosen||(n.kind!=="event"&&(related||visible.length<30))){ctx.font="12px sans-serif";ctx.fillStyle="#dce7f6";ctx.fillText(n.label.slice(0,16),p.x+10,p.y-7);}
    }
    ctx.globalAlpha=1;
  }
  function rows() {
    const box=el("rows");box.replaceChildren();
    for(const n of visible.slice(0,limit)){
      const li=node("li",""),button=node("button",n.label||"未记录正文");button.type="button";
      if(n.id===selected)button.className="view-selected";
      button.append(node("small",[names[n.kind],n.source,names[n.status],n.at?.slice(0,10)].filter(Boolean).join(" · ")));
      button.onclick=()=>select(n.id);li.append(button);box.append(li);
    }
    el("more").hidden=limit>=visible.length;
    el("list-count").textContent=`找到 ${visible.length} 个节点，列表显示 ${Math.min(limit,visible.length)} 个。`;
  }
  function filter() {
    if(!snapshot)return;
    const q=el("search").value.trim().toLowerCase(), kind=el("kind").value;
    visible=snapshot.nodes.filter(n=>(!kind||n.kind===kind)&&(el("state").value==="all"||!n.status||n.status==="active")&&
      [n.id,n.memory_id,n.label,n.text,n.note,n.who,n.source].join(" ").toLowerCase().includes(q));
    visible.sort((a,b)=>(b.at||"").localeCompare(a.at||"")||a.id.localeCompare(b.id));
    el("empty").hidden=visible.length>=3;
    el("empty").textContent=visible.length?"还连不成星座，先从下方列表看看。":"这里还没有匹配的记忆。";
    el("space").hidden=listOnly;
    el("toggle-list").textContent=listOnly?"星图视图":"列表视图";
    el("toggle-list").setAttribute("aria-pressed",String(listOnly));
    rows();draw();
  }
  async function select(id) {
    selected=id;rows();draw();
    const box=el("detail");box.replaceChildren(node("p","正在读取这颗星…","dim"));
    try {
      const response=await panelFetch("/api/memory/"+encodeURIComponent(id)+"/why",{method:"GET"});
      const data=await response.json();if(!response.ok)throw new Error(data.message||"详情暂时读不到");
      if(selected!==id)return;
      const query=new URLSearchParams(window.location.search);
      query.set("memory",id);
      window.history.replaceState(null,"","?"+query.toString()+window.location.hash);
      const n=data.node;box.replaceChildren(node("h2",n.kind==="event"?"一段记忆":n.label));
      if(n.kind==="event"){
        box.append(node("p",n.quote?"原话":"记录正文（不是原话）","dim"),node("pre",n.quote||n.text||"未记录"));
        if(n.note)box.append(node("p","她的理解","dim"),node("pre",n.note));
        const dl=node("dl","");
        const who=n.side==="她说"&&n.who==="她说"?"未记录":n.who;
        for(const [label,value] of [["来源",n.source],["时间",n.at],[n.side==="她说"?"交流对象":"记录归属",who],["记录视角",n.side],["状态",names[n.status]||n.status],
          ["状态变更时间",n.status_changed_at||"未记录"],
          ["所在层",names[n.tier]||n.tier],["清楚度",names[n.clarity]],["保留读数",n.retention],["证据编号",n.evidence_key||"没有原话证据"],
          ["召回次数",n.recall_count],["命中次数",n.hit_count],["最近召回",n.last_recalled_at||"未记录"]]){
          dl.append(node("dt",label),node("dd",value??"未记录"));
        }
        box.append(dl);
        if(n.turn_id){
          const links=node("p","");
          for(const [kind,label] of [["chat","查看当时对话"],["model","查看当轮模型调用"]]){
            const link=node("a",label);
            link.href=window.PANEL_BASE+"/observe?source="+kind+"&turn_id="+encodeURIComponent(n.turn_id);
            if(links.children.length)links.append(node("span"," · "));
            links.append(link);
          }
          box.append(links);
        }else box.append(node("p","这条记忆没有记录聊天回合，无法直接追溯。","dim"));
      } else box.append(node("p",n.id,"dim"));
      box.append(node("h2","已有的关联"));
      const list=node("div","","neighbors"),seen=new Set();
      for(const edge of data.edges.slice().sort((a,b)=>(b.ts||"").localeCompare(a.ts||""))){
        const other=edge.from===id?edge.to:edge.from, key=other+":"+edge.status;
        if(seen.has(key))continue;seen.add(key);
        const attribution=edge.type==="record-person";
        const button=node("button",(byId.get(other)?.label||other)+(edge.status==="active"?"":attribution?" · 记忆已退役或入冷库":" · 关系已失效"));
        button.append(node("small",[{"person-event":"人—记忆","event-topic":"记忆—话题","person-topic":"人—话题","record-person":"记录归属"}[edge.type]||edge.type,
          edge.ts, attribution?"依据已保存的交流对象，不代表原作者或亲历者":"权重 "+edge.weight].filter(Boolean).join(" · "),"dim"));
        button.type="button";button.onclick=()=>select(other);list.append(button);
      }
      if(!seen.size)list.append(node("p","还没有记录到关联；不按相似文字自动连线。","dim"));
      box.append(list,node("p",data.why.note,"dim"));
    } catch(error){if(selected===id)box.replaceChildren(node("p",error.message));}
  }
  async function refresh() {
    if(pending||document.hidden)return;
    clearTimeout(timer);pending=true;
    try{
      const response=await panelFetch("/api/graph/snapshot",{method:"GET"});const data=await response.json();
      if(!response.ok)throw new Error(data.message||"记忆暂时读不到");
      if(!snapshot||snapshot.revision!==data.revision){
        snapshot=data;byId=new Map(data.nodes.map(n=>[n.id,n]));
        if(selected&&!byId.has(selected)){selected="";el("detail").replaceChildren(node("p","之前选中的节点已不在当前数据中。"));}
        filter();if(selected)select(selected);
      }
      el("status").dataset.error="false";
      el("status").textContent=`${data.nodes.length} 个真实节点 · ${data.edges.length} 条关系与记录归属 · 每 5 秒更新`+
        (data.nodes.length>500?" · 画布最多 500 个，优先人物、话题与选中关联；其余可在列表查看":"")+
        (data.unresolved_edges?` · ${data.unresolved_edges} 条关系的原记录缺失，未绘制`:"")+
        (!ctx?" · 星图画不出来，先给你列表（浏览器未提供画布）":"");
    }catch(error){el("status").dataset.error="true";el("status").textContent=error.message+(snapshot?"；保留上次成功读取的画面。":"");el("empty").textContent="暂时读不到记忆，请稍后重试。";}
    finally{pending=false;if(!document.hidden)timer=setTimeout(refresh,5000);}
  }
  for(const id of ["search","kind","state"])el(id).addEventListener("input",()=>{limit=50;filter();});
  el("more").onclick=()=>{limit+=50;rows();};
  el("reset").onclick=()=>{camera={yaw:.4,pitch:.85,dist:1500};draw();};
  el("toggle-list").onclick=()=>{listOnly=ctx?!listOnly:true;filter();};
  canvas.addEventListener("pointerdown",event=>{dragging={x:event.clientX,y:event.clientY,moved:0};canvas.setPointerCapture(event.pointerId);});
  canvas.addEventListener("pointermove",event=>{if(!dragging)return;const dx=event.clientX-dragging.x,dy=event.clientY-dragging.y;
    dragging.moved+=Math.abs(dx)+Math.abs(dy);dragging.x=event.clientX;dragging.y=event.clientY;
    camera.yaw+=dx*.006;camera.pitch=Math.max(.1,Math.min(1.5,camera.pitch+dy*.006));draw();});
  canvas.addEventListener("pointerup",event=>{if(!dragging)return;const click=dragging.moved<5;dragging=null;if(!click)return;
    const rect=canvas.getBoundingClientRect(),x=event.clientX-rect.left,y=event.clientY-rect.top;
    const hit=plotted.map(p=>({p,d:Math.hypot(p.x-x,p.y-y)})).filter(p=>p.d<14).sort((a,b)=>a.d-b.d)[0];if(hit)select(hit.p.node.id);});
  canvas.addEventListener("pointercancel",()=>{dragging=null;});
  canvas.addEventListener("wheel",event=>{event.preventDefault();camera.dist=Math.max(600,Math.min(3600,camera.dist*Math.exp(event.deltaY*.001)));draw();},{passive:false});
  window.addEventListener("resize",draw);
  document.addEventListener("visibilitychange",()=>{clearTimeout(timer);if(!document.hidden){draw();refresh();}});
  window.addEventListener("pagehide",()=>clearTimeout(timer));
  refresh();
}());
