(() => {
'use strict';
const $=(s,r=document)=>r.querySelector(s), $$=(s,r=document)=>[...r.querySelectorAll(s)];
const profiles=[
{id:'libtv:general',provider:'LibTV',label:'General Image Pro',enabled:true,ratios:['1:1','4:3','3:4','16:9'],resolutions:['1K','2K','4K']},
{id:'libtv:legacy',provider:'LibTV',label:'Legacy Vision',enabled:false,ratios:['1:1'],resolutions:['1K']},
{id:'lovart:nano',provider:'Lovart',label:'Nano Banana Pro',enabled:true,ratios:['1:1','3:4'],resolutions:['2K']},
{id:'api:seedream-demo',provider:'API',label:'Seedream Studio（虚构）',enabled:true,ratios:['1:1','4:3','3:4','16:9','9:16'],resolutions:['1K','2K']}
];
const SCHEMA_VERSION=6,STORE_KEY='image-hub-demo-v6',LEGACY_STORE_KEYS=['image-hub-demo-v5','image-hub-demo-v4'];
let sequence=0,state,store;
let drag=null,pan=null,connecting=null,miniDrag=null,miniGeo=null,context=null,activePicker='',pointerSelectionId='',lastCanvasPointer=null,resultClickTimer=null;
const acceptedImageTypes=new Set(['image/png','image/jpeg','image/webp']),maxLocalImageBytes=30*1024*1024,imageOffset=32,urlOnlyMessage='暂不支持仅粘贴图片链接，请复制图片本身或下载后拖入',localFiles=new Map(),objectUrls=new Map();
const viewImage={scale:1,x:0,y:0,fit:true,pointer:null,sx:0,sy:0,ox:0,oy:0,invoker:null};
const uid=p=>`${p}-${Date.now().toString(36)}-${++sequence}`;
const esc=v=>{const d=document.createElement('div');d.textContent=String(v??'');return d.innerHTML};
const toast=(t,error=false)=>{$('#toast').textContent=t;$('#toast').className=`toast show${error?' error':''}`;setTimeout(()=>$('#toast').className='toast',2400)};
const icons={open:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5h5v5M19 5l-8 8"/><path d="M17 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V8a1 1 0 0 1 1-1h5"/></svg>',download:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m-4-4 4 4 4-4M5 19h14"/></svg>',remove:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M9 7V4h6v3m-8 0 1 13h8l1-13M10 11v5m4-5v5"/></svg>',chevron:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg>',close:'<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg>'};
const profile=id=>profiles.find(p=>p.id===id)||profiles.find(p=>p.enabled);
const combos=p=>p.ratios.flatMap(r=>p.resolutions.map(s=>({r,s,v:`${r}|${s}`})));
const clampCount=v=>Math.max(1,Math.min(4,Number(v)||1));
const ratioAspect=r=>String(r||'4:3').replace(':','/');
const sentimentNames={adopted:'采用',satisfied:'满意',dissatisfied:'不满意'};

/* ---- canonical unified generation node ---------------------------------- */
const isImage=n=>n?.type==='image';
const isGeneration=n=>n?.type==='generation_node';
const refFor=(nodeId,resultId)=>`result:${nodeId}:${resultId}`;
function parseRef(ref){if(typeof ref!=='string'||!ref.startsWith('result:'))return null;const rest=ref.slice(7),at=rest.lastIndexOf(':');if(at<=0||at===rest.length-1)return null;return{nodeId:rest.slice(0,at),resultId:rest.slice(at+1)}}
const refOwner=ref=>parseRef(ref)?.nodeId||ref;
const batchResults=b=>Array.isArray(b?.results)?b.results:[];
const nodeResults=n=>[...batchResults(n?.activeBatch),...batchResults(n?.attempt)];
function normalizeResult(r){return{id:r.id||uid('result'),generationId:r.generationId||'',artifactUrl:r.artifactUrl||r.src||'',status:r.status||'queued',error:r.error||'',sentiment:r.sentiment||'',aspect:r.aspect||ratioAspect(r.parameters?.ratio),provider:r.provider||'',model:r.model||r.modelLabel||'',prompt:r.prompt||'',profileId:r.profileId||'',parameters:{...(r.parameters||{})}}}
function normalizeRequest(r){return{prompt:r?.prompt||'',profileId:r?.profileId||'',provider:r?.provider||'',ratio:r?.ratio||'1:1',resolution:r?.resolution||'2K',count:clampCount(r?.count),inputs:[...new Set((r?.inputs||[]).filter(Boolean))]}}
function normalizeBatch(b){if(!b||typeof b!=='object')return null;const results=batchResults(b).map(normalizeResult);if(!results.length)return null;return{id:b.id||uid('batch'),request:normalizeRequest(b.request),results,settled:b.settled!==false}}
function normalizeNode(n){const node={...n,type:'generation_node',w:Number(n.w)||344,expanded:n.expanded===true,expandedPinned:n.expandedPinned===true,prompt:n.prompt||'',provider:n.provider||'',profileId:n.profileId||'',ratio:n.ratio||'1:1',resolution:n.resolution||'2K',count:clampCount(n.count),inputs:[...new Set((n.inputs||[]).filter(Boolean))],primaryResultId:n.primaryResultId||'',error:n.error||'',activeBatch:normalizeBatch(n.activeBatch),attempt:normalizeBatch(n.attempt)};if(node.attempt){node.attempt.settled=batchResults(node.attempt).every(r=>!['queued','running'].includes(r.status))}node.dirty=isDirty(node);return node}
function resolveRef(ref){const parsed=parseRef(ref);if(parsed){const node=state.nodes.find(n=>n.id===parsed.nodeId);if(!isGeneration(node))return null;const result=nodeResults(node).find(r=>r.id===parsed.resultId);if(!result?.artifactUrl)return null;return{ref,kind:'result',node,result,src:result.artifactUrl,title:`${result.model||'生成结果'} · ${result.parameters?.ratio||''}`.trim()}}const node=state.nodes.find(n=>n.id===ref);if(!isImage(node))return null;return{ref,kind:'image',node,result:null,src:node.src,title:node.title||'参考图片'}}
function displayBatch(node){if(batchResults(node.activeBatch).length)return node.activeBatch;if(batchResults(node.attempt).length)return node.attempt;return null}
function primaryOf(node){const batch=displayBatch(node);if(!batch)return null;const results=batchResults(batch);return results.find(r=>r.id===node.primaryResultId)||results.find(r=>r.status==='succeeded')||results[0]}
function primaryGenerationId(node){return primaryOf(node)?.generationId||''}
function isDirty(node){const request=node.attempt?.request||node.activeBatch?.request;if(!request)return false;return node.prompt!==request.prompt||node.profileId!==request.profileId||node.ratio!==request.ratio||node.resolution!==request.resolution||clampCount(node.count)!==request.count||node.inputs.join('|')!==request.inputs.join('|')}
function nodeStatus(node){if(node.submitting||(node.attempt&&!node.attempt.settled))return'processing';const results=batchResults(node.activeBatch);if(!results.length)return node.error?'failed':'empty';const ok=results.filter(r=>r.status==='succeeded').length;if(ok&&ok===results.length)return'succeeded';if(ok)return'partial';return'failed'}
function statusText(node){const status=nodeStatus(node),results=batchResults(node.activeBatch);if(status==='processing')return'生成中';if(status==='empty')return'待生成';if(status==='succeeded')return`已完成 ${results.length} 张`;if(status==='partial')return`已完成 ${results.filter(r=>r.status==='succeeded').length}/${results.length} 张`;return'生成失败'}
function snapshot(node){return normalizeRequest({prompt:node.prompt,profileId:node.profileId,provider:node.provider,ratio:node.ratio,resolution:node.resolution,count:node.count,inputs:node.inputs})}

/* ---- legacy canvas migration (idempotent) ------------------------------- */
function migrateNodes(nodes){
  const list=(Array.isArray(nodes)?nodes:[]).filter(n=>n&&typeof n==='object');
  if(!list.some(n=>n.type==='request'||n.type==='result'))return list.map(n=>({...n}));
  const plan=new Map();
  list.forEach(n=>{if(n.type==='request')plan.set(n.id,{request:n,results:[]})});
  const orphans=[];
  list.forEach(n=>{if(n.type!=='result')return;const owner=n.requestId?plan.get(n.requestId):null;if(owner)owner.results.push(n);else orphans.push(n)});
  orphans.forEach((r,i)=>plan.set(`generation-${r.id}`,{results:[r],fallback:{x:180+(i%3)*300,y:150+Math.floor(i/3)*380}}));
  const refByLegacy=new Map();
  plan.forEach((entry,nodeId)=>entry.results.forEach(r=>refByLegacy.set(r.id,refFor(nodeId,r.id))));
  const remap=inputs=>[...new Set((inputs||[]).map(ref=>refByLegacy.get(ref)||ref))];
  const out=[];
  list.forEach(n=>{
    if(n.type==='result')return;
    if(n.type==='request'){
      const entry=plan.get(n.id)||{results:[]};
      const inputs=remap(n.inputs);
      const results=entry.results.map(normalizeResult);
      out.push(normalizeNode({...n,type:'generation_node',inputs,expanded:results.length?false:n.expanded!==false,activeBatch:results.length?{id:`batch-${n.id}`,request:normalizeRequest({...n,inputs}),results}:null,primaryResultId:results.length?(results.find(r=>r.status==='succeeded')||results[0]).id:''}));
      return;
    }
    out.push({...n});
  });
  orphans.forEach(r=>{
    const nodeId=`generation-${r.id}`,single=normalizeResult(r),params=single.parameters||{};
    const request=normalizeRequest({prompt:single.prompt,profileId:single.profileId,provider:single.provider,ratio:params.ratio||'4:3',resolution:params.resolution||'2K',count:1,inputs:[]});
    out.push(normalizeNode({id:nodeId,type:'generation_node',x:plan.get(nodeId).fallback.x,y:plan.get(nodeId).fallback.y,w:344,expanded:false,...request,activeBatch:{id:`batch-${nodeId}`,request,results:[single]},primaryResultId:single.id}));
  });
  return out;
}
function restoreNodes(nodes){return (Array.isArray(nodes)?nodes:[]).map(n=>{if(!n||typeof n!=='object')return n;if(isImage(n)){const copy={...n};if(copy.localOnly){copy.src='';copy.needsReselect=true}return copy}return isGeneration(n)?normalizeNode(n):{...n}})}

/* ---- one card per result (mirrors the workspace contract) ---------------
   A card that produced several images is split into sibling cards once the
   batch settles, so every image can be opened and downloaded on its own. Only
   presentation state moves: each result stays one immutable Generation. */
const SPLIT_GAP=120,SPLIT_ROW_HEIGHT=420,SPLIT_MAX_COLUMNS=3;
function splitCandidate(node){
  if(!isGeneration(node))return null;
  if(node.attempt&&!node.attempt.settled)return null;
  const batches=batchResults(node.activeBatch).length?[node.activeBatch]:[node.attempt];
  for(const batch of batches){
    const results=batchResults(batch);
    if(results.length<=1)continue;
    if(results.some(r=>['queued','running'].includes(r.status)))continue;
    if(!results.some(r=>r.status==='succeeded'))continue;
    return batch;
  }
  return null;
}
/* Right of the origin card first, wrapping into tidy rows; inside the current
   viewport the sequence continues on the row below rather than covering the
   origin card. Positions are computed once and then persisted. */
function splitGrid(origin,count){
  const width=origin.w,gap=SPLIT_GAP,originX=origin.x,originY=origin.y;
  let columns=SPLIT_MAX_COLUMNS,startX=originX+width+gap,startY=originY;
  const vp=state&&$('#viewport');
  if(vp){
    const rect=vp.getBoundingClientRect(),right=-state.view.x/state.view.z+rect.width/state.view.z;
    const room=Math.floor((right-startX+gap)/(width+gap));
    columns=Math.max(1,Math.min(columns,room||1));
    if(room<1){startX=Math.min(originX,Math.max(-state.view.x/state.view.z,right-width));startY=originY+SPLIT_ROW_HEIGHT+gap}
  }
  return Array.from({length:count},(_,i)=>({x:startX+(i%columns)*(width+gap),y:startY+Math.floor(i/columns)*(SPLIT_ROW_HEIGHT+gap)}));
}
function splitRecipe(node){return normalizeRequest({prompt:node.prompt,profileId:node.profileId,provider:node.provider,ratio:node.ratio,resolution:node.resolution,count:1,inputs:node.inputs})}
function splitNodes(nodes){
  const list=Array.isArray(nodes)?nodes:[],remap=new Map(),created=[];let splits=0;
  list.forEach(node=>{
    const batch=splitCandidate(node);if(!batch)return;
    const results=batchResults(batch),first=results[0],rest=results.slice(1),recipe=splitRecipe(node);
    node.activeBatch={...batch,request:recipe,results:[first]};
    if(batch===node.attempt)node.attempt=null;
    node.count=1;node.primaryResultId=first.id;node.dirty=isDirty(node);
    const positions=splitGrid(node,rest.length);
    rest.forEach((result,index)=>{
      const id=uid('generation');remap.set(`${node.id}:${result.id}`,id);
      created.push(normalizeNode({id,type:'generation_node',x:positions[index].x,y:positions[index].y,w:node.w,expanded:false,expandedPinned:false,error:'',prompt:recipe.prompt,provider:recipe.provider,profileId:recipe.profileId,ratio:recipe.ratio,resolution:recipe.resolution,count:1,inputs:recipe.inputs,activeBatch:{id:`${batch.id||id}-split-${index+2}`,request:recipe,results:[result]},primaryResultId:result.id}));
    });
    splits++;
  });
  if(!splits)return{nodes:list,created:[],changed:false,splits:0};
  const all=[...list,...created];
  if(remap.size)all.forEach(node=>{if(!isGeneration(node)||!node.inputs.length)return;node.inputs=node.inputs.map(ref=>{const parsed=parseRef(ref),target=parsed?remap.get(`${parsed.nodeId}:${parsed.resultId}`):'';return target?refFor(target,parsed.resultId):ref})});
  return{nodes:all,created,changed:true,splits};
}
function splitSettled(){const outcome=splitNodes(state.nodes);if(!outcome.changed)return false;state.nodes=outcome.nodes;render();save();toast(`多图已拆分为 ${outcome.created.length+outcome.splits} 张独立卡片，每张可单独下载 PNG`);return true}

/* ---- initial project ---------------------------------------------------- */
function seeds(){
  const request=normalizeRequest({prompt:'保留产品比例，使用自然侧光与克制的浅灰背景',profileId:'api:seedream-demo',provider:'API',ratio:'4:3',resolution:'2K',count:1,inputs:['landscape']});
  const node=normalizeNode({id:'generation-a',type:'generation_node',x:560,y:140,w:344,expanded:false,prompt:request.prompt,provider:request.provider,profileId:request.profileId,ratio:request.ratio,resolution:request.resolution,count:request.count,inputs:request.inputs,primaryResultId:'result-a',activeBatch:{id:'batch-a',request,settled:true,results:[{id:'result-a',generationId:'demo-generation-a',artifactUrl:'./demo-result.svg',status:'succeeded',sentiment:'satisfied',provider:'API',model:'Seedream Studio（虚构）',prompt:request.prompt,profileId:request.profileId,parameters:{ratio:request.ratio,resolution:request.resolution,count:1},aspect:'4/3'}]}});
  return{id:'spring',name:'春季产品视觉',view:{x:0,y:0,z:1},selected:[],selectedLinks:[],clipboard:null,nodes:[{id:'landscape',type:'image',x:120,y:160,w:300,aspect:'3/1',naturalWidth:1200,naturalHeight:400,src:'./demo-landscape.svg',title:'fictional-landscape-3x1.svg'},node]};
}
function readStore(){
  try{const raw=localStorage.getItem(STORE_KEY);if(raw){const parsed=JSON.parse(raw);if(parsed?.projects?.length)return{store:parsed,migrated:false}}}catch{}
  for(const key of LEGACY_STORE_KEYS){
    try{const raw=localStorage.getItem(key);if(!raw)continue;const parsed=JSON.parse(raw);if(!parsed?.projects?.length)continue;parsed.projects.forEach(project=>{project.nodes=migrateNodes(project.nodes)});return{store:parsed,migrated:true}}catch{}
  }
  return{store:null,migrated:false};
}
const loaded=readStore();
store=loaded.store||{current:'spring',projects:[seeds()]};
if(!Array.isArray(store.projects)||!store.projects.length)store={current:'spring',projects:[seeds()]};
/* Loading is the idempotent migration point: a canvas saved before the split
   existed still holds multi-image cards, so it is split once here and the
   result is written back under the current schema version. */
let needsSave=loaded.migrated;
store.projects.forEach(project=>{project.version=SCHEMA_VERSION;project.nodes=restoreNodes(project.nodes);project.selectedLinks=Array.isArray(project.selectedLinks)?project.selectedLinks:[];const split=splitNodes(project.nodes);project.nodes=split.nodes;if(split.changed)needsSave=true});
state=store.projects.find(p=>p.id===store.current)||store.projects[0];
store.current=state.id;
const node=id=>state.nodes.find(n=>n.id===id);
const save=()=>{store.current=state.id;localStorage.setItem(STORE_KEY,JSON.stringify(store,function(key,value){return key==='src'&&this.localOnly?'':value}));$('.saved').innerHTML='<i></i>本地已保存'};
if(needsSave)save();
const point=(x,y)=>{const r=$('#viewport').getBoundingClientRect();return{x:(x-r.left-state.view.x)/state.view.z,y:(y-r.top-state.view.y)/state.view.z}};

/* ---- node creation and editing ----------------------------------------- */
function defaultGeneration(x,y,inherit={}){
  const p=profile(inherit.profileId),combo=combos(p).find(c=>c.r===inherit.ratio&&c.s===inherit.resolution)||combos(p)[0];
  return normalizeNode({id:uid('generation'),type:'generation_node',x,y,w:344,expanded:true,prompt:inherit.prompt||'',provider:p.provider,profileId:p.id,ratio:combo.r,resolution:combo.s,count:clampCount(inherit.count),inputs:inherit.inputs?[...inherit.inputs]:[],primaryResultId:''});
}
/* Every path that mutates the node collection must redraw explicitly: select()
   is presentation-only and no longer carries the implicit redraw that used to
   make a new card appear without a page reload. */
function addGeneration(x,y,inherit={}){const created=defaultGeneration(x,y,inherit);state.nodes.push(created);select(created.id,false);render();save();requestAnimationFrame(()=>$(`[data-id="${created.id}"] textarea`)?.focus());return created}
function addInput(nodeId,ref){const target=node(nodeId);if(!isGeneration(target))return false;const parsed=parseRef(ref);if(parsed&&parsed.nodeId===nodeId){toast('不能引用本节点自己的结果',true);return false}if(!resolveRef(ref))return false;if(target.inputs.includes(ref))return false;target.inputs.push(ref);syncDirty(target);render();save();return true}
function syncDirty(target){const next=isDirty(target);if(next===target.dirty)return;target.dirty=next}
function remove(ids){const gone=new Set(ids);gone.forEach(id=>{if(objectUrls.has(id))URL.revokeObjectURL(objectUrls.get(id));objectUrls.delete(id);localFiles.delete(id)});state.nodes=state.nodes.filter(n=>!gone.has(n.id)).map(n=>isGeneration(n)?{...n,inputs:n.inputs.filter(ref=>!gone.has(refOwner(ref)))}:n);state.selected=state.selected.filter(id=>!gone.has(id));state.selectedLinks=state.selectedLinks.filter(key=>!gone.has(linkTarget(key)));render();save()}
function select(id,shift){const target=node(id);if(!target)return;clearLinkSelection();if(shift)state.selected=state.selected.includes(id)?state.selected.filter(x=>x!==id):[...state.selected,id];else state.selected=[id];$$('.node').forEach(el=>{const on=state.selected.includes(el.dataset.id);el.classList.toggle('selected',on);el.setAttribute('aria-selected',String(on))})}

/* ---- input connection selection -----------------------------------------
   One connection equals one entry of the target card's `inputs` list, which is
   the same relationship the reference-thumbnail 「×」 button edits. Selecting a
   link never touches node selection and vice versa, so `Delete` has exactly one
   meaning at a time. */
function linkKey(source,target){return `${target}::${source}`}
function linkTarget(key){const at=key.indexOf('::');return at<0?'':key.slice(0,at)}
function linkSource(key){const at=key.indexOf('::');return at<0?'':key.slice(at+2)}
function liveLinkKeys(){return (state.selectedLinks||[]).filter(key=>{const target=node(linkTarget(key));return isGeneration(target)&&target.inputs.includes(linkSource(key))})}
/* Presentation-only, exactly like select(): toggling a class avoids
   re-rendering the SVG under the pointer. */
function syncLinkSelection(){$('#links').querySelectorAll('.input-link,.link-hit').forEach(el=>el.classList.toggle('selected',state.selectedLinks.includes(linkKey(el.dataset.source,el.dataset.target))))}
function clearLinkSelection(){if(!(state.selectedLinks||[]).length)return false;state.selectedLinks=[];syncLinkSelection();return true}
function selectLink(source,target,shift){if(!source||!target)return;const key=linkKey(source,target);if(state.selected.length){state.selected=[];$$('.node').forEach(el=>{el.classList.remove('selected');el.setAttribute('aria-selected','false')})}state.selectedLinks=shift?(state.selectedLinks.includes(key)?state.selectedLinks.filter(x=>x!==key):[...state.selectedLinks,key]):[key];syncLinkSelection()}
/* Removing a connection drops that one reference from the target card's input
   list. Everything that is not that exact entry keeps its relative position,
   and uploads, results and Generations are untouched. A key whose reference is
   already gone is a silent no-op. */
function removeLinks(keys){
  const byTarget=new Map();
  keys.forEach(key=>{const target=linkTarget(key),source=linkSource(key);if(!target||!source)return;if(!byTarget.has(target))byTarget.set(target,new Set());byTarget.get(target).add(source)});
  let removed=0;
  byTarget.forEach((sources,targetId)=>{
    const target=node(targetId);
    if(!isGeneration(target)||!Array.isArray(target.inputs))return;
    const next=target.inputs.filter(ref=>{if(!sources.has(ref))return true;removed++;return false});
    if(next.length===target.inputs.length)return;
    target.inputs=next;syncDirty(target);
  });
  if(!removed)return 0;
  state.selectedLinks=[];
  render();save();
  toast(removed===1?'已移除 1 条连线':`已移除 ${removed} 条连线`);
  return removed;
}
function copy(){if(!state.selected.length)return;state.clipboard={nodes:structuredClone(state.nodes),ids:[...state.selected]};save();toast(`已复制 ${state.selected.length} 个节点`)}
function paste(at){
  if(!state.clipboard?.ids.length)return;
  const all=state.clipboard.nodes,chosen=all.filter(n=>state.clipboard.ids.includes(n.id)),map=Object.fromEntries(chosen.map(n=>[n.id,uid(isImage(n)?'image':'generation')])),minX=Math.min(...chosen.map(n=>n.x)),minY=Math.min(...chosen.map(n=>n.y)),v=$('#viewport').getBoundingClientRect(),target=at||point(v.left+v.width/2,v.top+v.height/2);
  const clones=chosen.map(n=>{
    const clone={...n,id:map[n.id],x:n.x+target.x-minX+24,y:n.y+target.y-minY+24};
    if(isImage(clone)){if(localFiles.has(n.id)){const file=localFiles.get(n.id),src=URL.createObjectURL(file);localFiles.set(clone.id,file);objectUrls.set(clone.id,src);clone.src=src;clone.needsReselect=false}return clone}
    /* A duplicate carries the recipe only; generation evidence stays untouched. */
    clone.inputs=n.inputs.flatMap(ref=>{const owner=refOwner(ref);if(parseRef(ref)&&state.clipboard.ids.includes(owner))return[];return[map[owner]||ref]});
    clone.expanded=true;clone.error='';clone.dirty=false;clone.activeBatch=null;clone.attempt=null;clone.primaryResultId='';
    return normalizeNode(clone);
  });
  state.nodes.push(...clones);state.selected=clones.map(n=>n.id);state.selectedLinks=[];render();save();toast(`已粘贴 ${clones.length} 个节点`);
}

/* ---- markup ------------------------------------------------------------- */
function picker(n){const p=profile(n.profileId),groups=profiles.reduce((m,x)=>((m[x.provider]||=[]).push(x),m),{}),provider=n.pickerProvider||p.provider,open=activePicker===`${n.id}:model`,items=groups[provider]||[];return `<div class="visual-picker model-picker"><button class="selector-trigger" data-model-trigger aria-expanded="${open}" aria-haspopup="dialog"><span>${esc(p.provider)} · ${esc(p.label)}</span>${icons.chevron}</button><div class="selector-popover" role="dialog" aria-label="平台与模型" ${open?'':'hidden'}><header><b>平台与模型</b><button data-close-picker aria-label="关闭平台与模型">${icons.close}</button></header><section class="selector-section"><span>平台</span><div class="provider-chips" role="tablist">${Object.keys(groups).map(k=>`<button role="tab" data-provider="${k}" aria-selected="${k===provider}">${esc(k)}</button>`).join('')}</div></section><section class="selector-section"><span>模型</span><div class="model-card-grid" role="radiogroup">${items.map(x=>`<button role="radio" data-profile="${x.id}" aria-checked="${x.id===n.profileId}" ${x.enabled?'':'disabled'}><span>${esc(x.label)}</span><small>${x.enabled?(x.id===n.profileId?'已选择':'可用'):'未配置'}</small>${x.id===n.profileId?'<b>✓</b>':''}</button>`).join('')}</div></section></div></div>`}
function imagePicker(n){const p=profile(n.profileId),open=activePicker===`${n.id}:image`,preview=r=>`<span class="ratio-preview" style="--ratio:${r.replace(':','/')}" aria-hidden="true"></span>`;return `<div class="visual-picker"><button class="selector-trigger" data-image-trigger aria-expanded="${open}" aria-haspopup="dialog"><span>${n.ratio} · ${n.resolution}</span>${icons.chevron}</button><div class="selector-popover" role="dialog" aria-label="图像设置" ${open?'':'hidden'}><header><b>图像设置</b><button data-close-picker aria-label="关闭图像设置">${icons.close}</button></header><section class="selector-section"><span>分辨率</span><div class="resolution-chips" role="radiogroup">${p.resolutions.map(x=>`<button role="radio" data-resolution="${x}" aria-checked="${x===n.resolution}">${x}</button>`).join('')}</div></section><section class="selector-section"><span>宽高比</span><div class="ratio-card-grid" role="radiogroup">${p.ratios.map(x=>`<button role="radio" data-ratio="${x}" aria-checked="${x===n.ratio}">${preview(x)}<span>${x}</span></button>`).join('')}</div></section></div></div>`}
function inputRows(n){return n.inputs.map((ref,index)=>{const source=resolveRef(ref),label=`图${index+1}`;if(!source)return `<li class="missing" data-input="${esc(ref)}" data-owner="${n.id}"><span>${label} 已失效</span><button data-remove="${esc(ref)}" aria-label="移除${label}" title="移除${label}">×</button></li>`;return `<li draggable="true" data-input="${esc(ref)}" data-owner="${n.id}"><span class="input-index">${label}</span><img src="${esc(source.src)}" alt=""><button data-remove="${esc(ref)}" aria-label="移除${label}" title="移除${label}">×</button></li>`}).join('')}
function tile(n,result,primaryId,showTag){
  const ok=result.status==='succeeded'&&Boolean(result.artifactUrl);
  const media=ok?`<img src="${esc(result.artifactUrl)}" alt="生成结果" loading="lazy">`:`<div class="result-state ${esc(result.status)}"><strong>${esc(result.status==='running'?'生成中':result.status==='queued'?'排队中':'生成失败')}</strong></div>`;
  const isPrimary=Boolean(primaryId)&&primaryId===result.id;
  return `<div class="result-tile${isPrimary?' primary':''}${ok?' ready':' pending'}" data-result="${esc(result.id)}">${media}${isPrimary&&showTag?'<span class="primary-tag">主图</span>':''}${ok?`<button class="port tile-port" data-tile-ref="${esc(refFor(n.id,result.id))}" aria-label="从此结果创建连接"></button>`:''}</div>`;
}
function grid(n,batch,isActive){const results=batchResults(batch),single=results.length===1,primaryId=isActive?(primaryOf(n)?.id||''):'';return `<div class="result-grid${single?' single':''}" data-count="${results.length}"${single?` style="--media-ratio:${esc(results[0].aspect||'4/3')}"`:''}>${results.map(r=>tile(n,r,primaryId,results.length>1)).join('')}</div>`}
function progress(n){if(!(n.submitting||(n.attempt&&!n.attempt.settled)))return'';const results=batchResults(n.attempt),done=results.filter(r=>!['queued','running'].includes(r.status)).length,total=results.length||clampCount(n.count),keeping=batchResults(n.activeBatch).length?' · 保留上一版图片':'';return `<div class="batch-progress"><span class="spinner"></span><span>${n.submitting&&!done?'正在提交任务':`生成中 ${done}/${total}${keeping}`}</span><i class="progress-track"><b style="width:${Math.round(done/Math.max(1,total)*100)}%"></b></i></div>`}
function errorRow(n,batch){if(!n.error||nodeStatus(n)==='processing')return'';const retry=batch===n.attempt||!n.attempt?'':`<button class="retry-action" data-retry>安全重试</button>`;return `<p class="generation-error"><span>${esc(n.error)}</span>${retry}</p>`}
function editor(n){
  const p=profile(n.profileId),rows=inputRows(n),busy=n.submitting||Boolean(n.attempt&&!n.attempt.settled),hasBatch=Boolean(displayBatch(n));
  const primary=primaryOf(n);
  const sentiment=primary?.status==='succeeded'?`<div class="sentiment-row"><span>主图评价</span><div class="sentiment-group">${Object.entries(sentimentNames).map(([k,label])=>`<button data-sentiment="${k}" class="${primary.sentiment===k?'selected':''}" aria-pressed="${primary.sentiment===k}">${label}</button>`).join('')}</div></div>`:'';
  return `<div class="generation-editor">${rows?`<ol class="inputs">${rows}</ol>`:'<p class="no-input">无参考图，可直接文生图</p>'}<label class="prompt-label">提示词<textarea data-field="prompt">${esc(n.prompt)}</textarea></label><label>平台 · 模型${picker(n)}</label><div class="settings"><label>比例 · 分辨率${imagePicker(n)}</label><label>数量<select data-field="count">${[1,2,3,4].map(v=>`<option value="${v}" ${v===n.count?'selected':''}>${v} 张</option>`).join('')}</select></label></div>${sentiment}<button class="generate" data-generate ${busy?'disabled':''}>${n.submitting?'正在提交…':busy?'生成中…':hasBatch?'重新生成':'生成图片'}</button></div>`;
}
/* The rail owns the single result-count readout; the header only reports the
   input count (plus a status while no result summary exists yet). The card's
   only delete control lives in the header, and expanding the parameters is
   owned by a single click on the result (plus the header chevron for the
   keyboard path), so the rail keeps just the readout and the two file actions:
   the readout takes the free space and the actions sit flush right, which
   keeps the footer balanced without an empty slot. */
function rail(n){
  const batch=displayBatch(n);if(!batch)return'';
  const results=batchResults(batch),primary=primaryOf(n),ready=Boolean(primary?.status==='succeeded'&&primary.artifactUrl),ok=results.filter(r=>r.status==='succeeded').length;
  const meta=`${ok}/${results.length} 张结果${n.error?' · 有失败':''}`;
  return `<footer class="generation-rail"><span class="rail-meta" title="${esc(meta)}">${esc(meta)}</span><div class="image-actions"><div class="file-actions"><button data-open-result ${ready?'':'disabled'} aria-label="打开主图" title="打开主图">${icons.open}</button><button data-download-result ${ready?'':'disabled'} aria-label="下载主图" title="下载主图">${icons.download}</button></div></div></footer>`;
}
function generationMarkup(n){
  const active=batchResults(n.activeBatch).length?n.activeBatch:null,batch=active||(batchResults(n.attempt).length?n.attempt:null),selected=state.selected.includes(n.id);
  const inputMeta=`${n.inputs.length} 张输入${batch?'':` · ${esc(statusText(n))}`}`;
  const classes=['node','generation',n.expanded?'expanded':'',batch?'has-results':'',selected?'selected':''].filter(Boolean).join(' ');
  return `<article class="${classes}" data-id="${n.id}" data-node-type="generation_node" aria-selected="${selected}" tabindex="-1" style="left:${n.x}px;top:${n.y}px;width:${n.w}px"><header><div class="generation-heading"><b>生成</b><small>${inputMeta}</small><em class="dirty-flag" title="已修改，待重新生成；当前图片会保留。" ${n.dirty?'':'hidden'}>待重新生成</em></div><div class="node-header-actions"><button data-toggle aria-label="${n.expanded?'收起为结果简洁态':'展开编辑参数'}" title="${n.expanded?'收起为结果简洁态':'展开编辑参数'}" aria-expanded="${n.expanded}">${icons.chevron}</button><i></i><button class="request-remove" data-delete aria-label="从画布移除" title="从画布移除">${icons.remove}</button></div></header><div class="generation-body"><button class="port in" aria-label="参考图输入端口"></button><div class="generation-media">${batch?grid(n,batch,Boolean(active)):''}${progress(n)}${errorRow(n,batch)}</div>${n.expanded?editor(n):''}</div>${rail(n)}</article>`;
}
function imageMarkup(n){
  const missing=n.needsReselect||(n.localOnly&&!localFiles.has(n.id));
  const media=missing?'<div class="result-state"><strong>需要重新选择图片</strong><button data-reselect>重新选择图片</button></div>':`<img src="${esc(n.src||'')}" alt="${esc(n.title||'参考图片')}">`;
  const label=`${n.title||'参考图片'}${n.naturalWidth?` · ${n.naturalWidth} × ${n.naturalHeight}`:''}`;
  return `<article class="node image ${state.selected.includes(n.id)?'selected':''}" data-id="${n.id}" data-node-type="image" aria-selected="${state.selected.includes(n.id)}" tabindex="-1" style="left:${n.x}px;top:${n.y}px;width:${n.w}px"><div class="frame" style="aspect-ratio:${n.aspect}">${media}</div><footer><span title="${esc(label)}">${esc(label)}</span><div class="image-actions"><div class="file-actions">${missing?'':`<button data-open-image aria-label="打开原图">${icons.open}</button>`}</div><div class="remove-actions"><button data-delete aria-label="从画布移除">${icons.remove}</button></div></div></footer><button class="port out" aria-label="从这张图片创建连接"></button></article>`;
}
function render(){state.selected=state.selected.filter(id=>node(id));state.selectedLinks=liveLinkKeys();$('#nodes').innerHTML=state.nodes.map(n=>isGeneration(n)?generationMarkup(n):imageMarkup(n)).join('');$('#empty').hidden=state.nodes.length>0;apply();requestAnimationFrame(()=>{links();minimap()});renderProjects()}
/* The header chevron is the only control that returns a card to the collapsed
   result summary, so it also owns the expand pin. */
function toggleGeneration(n){if(!isGeneration(n))return;n.expanded=!n.expanded;n.expandedPinned=n.expanded;render();requestAnimationFrame(()=>requestAnimationFrame(()=>{links();minimap()}));save()}
/* Single click expands in place (and marks the primary result when the batch
   holds several images); double click is handled separately by the viewer.
   Expanding here is a user action, so it pins the card against the automatic
   collapse that settling a generation may attempt. */
function activateResult(n,batch,result){let changed=false;const results=batchResults(batch);if(results.length>1&&n.primaryResultId!==result.id){n.primaryResultId=result.id;changed=true}if(!n.expanded){n.expanded=true;changed=true}if(!n.expandedPinned){n.expandedPinned=true;changed=true}if(changed)render();save()}
function apply(){$('#world').style.transform=`translate(${state.view.x}px,${state.view.y}px) scale(${state.view.z})`;$('#zoom').textContent=`${Math.round(state.view.z*100)}%`}
function portPoint(id,sel){const p=$(`[data-id="${id}"] ${sel}`),v=$('#viewport');if(!p)return null;const a=p.getBoundingClientRect(),b=v.getBoundingClientRect();return{x:(a.left+a.width/2-b.left-state.view.x)/state.view.z,y:(a.top+a.height/2-b.top-state.view.y)/state.view.z}}
const curve=(a,b)=>{const q=Math.max(70,Math.abs(b.x-a.x)*.45);return`M ${a.x} ${a.y} C ${a.x+q} ${a.y}, ${b.x-q} ${b.y}, ${b.x} ${b.y}`};
/* A 1.25px stroke is unhittable with a mouse, so every connection renders a
   second, invisible hit path sharing the identical `d`. The hit path keeps
   `vector-effect: non-scaling-stroke`, so its 14px grab band stays 14 screen
   pixels at any zoom. Both paths sit in `#links`, which stays
   `pointer-events: none`: only the hit path re-enables pointer events, and
   `#nodes` is a later sibling so nodes, ports and the toolbar are never
   covered. Port drags and the temporary link get no hit path. */
function links(){
  let h='';
  const isSelected=key=>state.selectedLinks.includes(key);
  state.nodes.filter(isGeneration).forEach(n=>n.inputs.forEach((ref,i)=>{
    const parsed=parseRef(ref),from=parsed?$(`[data-id="${parsed.nodeId}"] [data-result="${parsed.resultId}"] .tile-port`):$(`[data-id="${ref}"] .out`),to=$(`[data-id="${n.id}"] .in`),v=$('#viewport');
    if(!from||!to)return;
    const b=v.getBoundingClientRect(),c=e=>{const r=e.getBoundingClientRect();return{x:(r.left+r.width/2-b.left-state.view.x)/state.view.z,y:(r.top+r.height/2-b.top-state.view.y)/state.view.z}};
    const path=curve(c(from),c(to)),key=linkKey(ref,n.id),sel=isSelected(key)?' selected':'',meta=`data-source="${esc(ref)}" data-target="${n.id}" data-order="${i+1}"`;
    h+=`<path class="input-link${sel}" ${meta} data-start-x="${c(from).x}" data-start-y="${c(from).y}" data-end-x="${c(to).x}" data-end-y="${c(to).y}" d="${path}"></path>`;
    h+=`<path class="link-hit${sel}" ${meta} d="${path}"></path>`;
  }));
  if(connecting)h+=`<path class="temp" d="${curve(connecting.start,connecting.current)}"></path>`;
  $('#links').innerHTML=h;
}
function rects(){return state.nodes.map(n=>{const r=$(`[data-id="${n.id}"]`)?.getBoundingClientRect();return{x:n.x,y:n.y,width:n.w,height:r?r.height/state.view.z:240,type:n.type}})}
function miniGeometry(rs,visible){const all=[...rs,visible],m=80,minX=Math.min(...all.map(r=>r.x))-m,minY=Math.min(...all.map(r=>r.y))-m,maxX=Math.max(...all.map(r=>r.x+r.width))+m,maxY=Math.max(...all.map(r=>r.y+r.height))+m,s=Math.min(160/(maxX-minX),96/(maxY-minY)),ox=(176-(maxX-minX)*s)/2-minX*s,oy=(112-(maxY-minY)*s)/2-minY*s,map=r=>({x:r.x*s+ox,y:r.y*s+oy,w:Math.max(2,r.width*s),h:Math.max(2,r.height*s)});return{s,ox,oy,nodes:rs.map(map),viewport:map(visible)}}
function minimap(){const mapEl=$('#minimap-map');if(!mapEl)return;const v=$('#viewport').getBoundingClientRect(),visible={x:-state.view.x/state.view.z,y:-state.view.y/state.view.z,width:v.width/state.view.z,height:v.height/state.view.z},rs=rects();miniGeo=miniGeometry(rs,visible);mapEl.dataset.nodeCount=String(rs.length);if(mapEl.hidden)return;mapEl.innerHTML=miniGeo.nodes.map((r,i)=>`<rect class="mini-node ${rs[i].type}" x="${r.x}" y="${r.y}" width="${r.w}" height="${r.h}"/>`).join('')+`<rect class="mini-viewport" x="${miniGeo.viewport.x}" y="${miniGeo.viewport.y}" width="${miniGeo.viewport.w}" height="${miniGeo.viewport.h}"/>`}
function miniPan(e){if(!miniGeo)return;const r=$('#minimap-map').getBoundingClientRect(),mx=(e.clientX-r.left)*176/r.width,my=(e.clientY-r.top)*112/r.height,w={x:(mx-miniGeo.ox)/miniGeo.s,y:(my-miniGeo.oy)/miniGeo.s},v=$('#viewport').getBoundingClientRect();state.view.x=v.width/2-w.x*state.view.z;state.view.y=v.height/2-w.y*state.view.z;apply();links();minimap()}
function syncDragging(){$('#viewport')?.classList.toggle('dragging',Boolean(drag||pan||connecting))}
function clearPointer(pointer){let changed=false;if(drag&&(pointer==null||drag.pointer===pointer)){$$('[data-id]').forEach(el=>el.classList.remove('dragging'));drag=null;changed=true}if(pan&&(pointer==null||pan.pointer===pointer)){pan=null;changed=true}syncDragging();return changed}
function zoom(next,x,y){closeMenu();const r=$('#viewport').getBoundingClientRect(),old=state.view.z,z=Math.max(.4,Math.min(1.7,next)),px=x-r.left,py=y-r.top,wx=(px-state.view.x)/old,wy=(py-state.view.y)/old;state.view={x:px-wx*z,y:py-wy*z,z};apply();links();minimap();save()}
function fit(){if(!state.nodes.length){state.view={x:0,y:0,z:1};apply();return}const rs=rects(),v=$('#viewport').getBoundingClientRect(),minX=Math.min(...rs.map(n=>n.x)),minY=Math.min(...rs.map(n=>n.y)),maxX=Math.max(...rs.map(n=>n.x+n.width)),maxY=Math.max(...rs.map(n=>n.y+n.height)),z=Math.max(.4,Math.min(1,(v.width-100)/(maxX-minX),(v.height-100)/(maxY-minY)));state.view={x:50-minX*z,y:50-minY*z,z};apply();links();minimap();save()}
function transferFiles(source){const files=[...(source?.files||[])],items=[...(source?.items||[])].filter(item=>item.kind==='file').map(item=>item.getAsFile?.()).filter(Boolean);return[...new Set([...files,...items])]}
function pasteEditable(target){const active=document.activeElement;return!!(target?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"])')||active?.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"]),dialog form'))}
function hasUrlOnlyTransfer(source){const uri=source?.getData?.('text/uri-list')||'',text=source?.getData?.('text/plain')||'',html=source?.getData?.('text/html')||'';return!!(uri.trim()||/^https?:\/\/\S+$/i.test(text.trim())||/<img\b[^>]*\bsrc\s*=/i.test(html))}
function imageSize(src){return new Promise((ok,fail)=>{const im=new Image();im.onload=()=>ok([im.naturalWidth,im.naturalHeight]);im.onerror=()=>fail(new Error('无法读取图片，请确认文件未损坏'));im.src=src})}
async function importExternalImages(source,p,options={}){const candidates=Array.isArray(source)?source:transferFiles(source);if(!candidates.length){if(options.warnUrl&&hasUrlOnlyTransfer(source))toast(urlOnlyMessage,true);return 0}const valid=[];let unsupported=false,oversized=false;candidates.forEach((candidate,i)=>{const file=candidate instanceof File?candidate:new File([candidate],`clipboard-image-${i+1}`,{type:candidate.type});if(!acceptedImageTypes.has(file.type.toLowerCase()))unsupported=true;else if(file.size>maxLocalImageBytes)oversized=true;else valid.push(file)});let imported=0;for(const file of valid){const id=uid('image'),src=URL.createObjectURL(file);try{const size=await imageSize(src),i=imported;state.nodes.push({id,type:'image',x:p.x+i*imageOffset,y:p.y+i*imageOffset,w:270,aspect:`${size[0]}/${size[1]}`,naturalWidth:size[0],naturalHeight:size[1],src,title:file.name||'粘贴的图片',localOnly:true,needsReselect:false});localFiles.set(id,file);objectUrls.set(id,src);imported++}catch(error){URL.revokeObjectURL(src);toast(error.message,true)}}if(imported){render();save()}if(oversized)toast('单张图片不能超过 30 MB',true);else if(unsupported)toast('仅支持 PNG、JPEG 和 WebP 图片',true);return imported}
function reselectImage(n){const input=document.createElement('input');input.type='file';input.accept='image/png,image/jpeg,image/webp';input.onchange=async()=>{const file=input.files?.[0];if(!file)return;if(!acceptedImageTypes.has(file.type.toLowerCase())){toast('仅支持 PNG、JPEG 和 WebP 图片',true);return}if(file.size>maxLocalImageBytes){toast('单张图片不能超过 30 MB',true);return}const src=URL.createObjectURL(file);try{const size=await imageSize(src);if(objectUrls.has(n.id))URL.revokeObjectURL(objectUrls.get(n.id));localFiles.set(n.id,file);objectUrls.set(n.id,src);Object.assign(n,{src,title:file.name,aspect:`${size[0]}/${size[1]}`,naturalWidth:size[0],naturalHeight:size[1],localOnly:true,needsReselect:false});render();save()}catch(error){URL.revokeObjectURL(src);toast(error.message,true)}};input.click()}

/* ---- in-place generation simulation (no paid calls) --------------------- */
function settleAttempt(n){
  const attempt=n.attempt;if(!attempt||attempt.settled)return false;
  const results=batchResults(attempt);if(!results.length||results.some(r=>['queued','running'].includes(r.status)))return false;
  attempt.settled=true;
  const ok=results.some(r=>r.status==='succeeded'),previous=batchResults(n.activeBatch);
  if(ok||!previous.length){
    n.activeBatch={id:attempt.id,request:attempt.request,results};n.attempt=null;
    n.error=ok?'':(results.map(r=>r.error).find(Boolean)||'生成失败，可安全重试');
    n.primaryResultId=ok?(results.find(r=>r.status==='succeeded')?.id||''):'';
    n.dirty=isDirty(n);
    /* Only a freshly submitted generation may return the card to its result
       summary. A card the user expanded by hand keeps its editor open until
       the header toggle closes it. */
    if(!ok){n.expanded=true;n.expandedPinned=true}
    else if(!n.expandedPinned)n.expanded=false;
    splitSettled();
    return true;
  }
  n.error=results.map(r=>r.error).find(Boolean)||'生成失败，可安全重试';n.dirty=isDirty(n);return true;
}
function generate(n){
  if(!isGeneration(n))return;
  if(n.submitting||(n.attempt&&!n.attempt.settled)){toast('该节点正在生成，请等待当前任务结束',true);return}
  if(!n.prompt.trim()){toast('请先输入提示词');$(`[data-id="${n.id}"] textarea`)?.focus();return}
  const p=profile(n.profileId),request=snapshot(n),count=clampCount(request.count),aspect=ratioAspect(request.ratio);
  const results=Array.from({length:count},(_,i)=>({id:uid('result'),generationId:uid('demo-generation'),artifactUrl:'',status:'queued',error:'',sentiment:'',aspect,provider:p.provider,model:p.label,prompt:n.prompt,profileId:p.id,parameters:{ratio:request.ratio,resolution:request.resolution,count}}));
  /* Submitting is an explicit "show me the new result" intent, so the card is
     allowed to fall back to its result summary once this batch settles. */
  n.attempt={id:uid('batch'),request,results,settled:false};n.submitting=true;n.error='';n.expandedPinned=false;render();save();
  /* One immutable Generation per result on the real backend; the demo only
     advances local state inside the same node. */
  n.submitting=false;
  let index=0;
  const step=()=>{
    if(index>=results.length){settleAttempt(n);render();save();toast('虚构结果已完成，付费调用 0');return}
    const result=results[index];result.status='running';render();
    setTimeout(()=>{result.status='succeeded';result.artifactUrl=index%2?'./demo-portrait.svg':'./demo-result.svg';index++;render();step()},420);
  };
  setTimeout(step,200);
}
function retry(n){const attempt=n.attempt;if(!attempt)return;attempt.settled=false;attempt.results.forEach(r=>{r.status='queued';r.error=''});n.error='';render();save();let index=0;const step=()=>{if(index>=attempt.results.length){settleAttempt(n);render();save();return}const result=attempt.results[index];result.status='running';render();setTimeout(()=>{result.status='succeeded';result.artifactUrl=index%2?'./demo-portrait.svg':'./demo-result.svg';index++;render();step()},380)};setTimeout(step,200)}
function setSentiment(n,value,button){const primary=primaryOf(n);if(!primary)return;primary.sentiment=value;render();save();toast(`已标记为${sentimentNames[value]}`);void button}

/* ---- PNG download without a backend -------------------------------------
   The demo images are same-origin SVG placeholders, so drawing them onto a
   canvas and re-encoding keeps the download a real PNG. A tainted canvas (for
   example when the file is opened straight from disk) or a failed decode is
   reported instead of silently doing nothing. */
function pngName(url){const base=String(url||'').split('?')[0].split('/').filter(Boolean).pop()||'';return `${base.replace(/\.[a-z0-9]+$/i,'')||'xgm-ai-image-hub'}.png`}
function downloadPng(url){
  if(!url)return;
  const image=new Image();
  image.onload=()=>{
    try{
      const width=image.naturalWidth||1600,height=image.naturalHeight||1200;
      const canvas=document.createElement('canvas');canvas.width=width;canvas.height=height;
      const context=canvas.getContext('2d');
      if(!context)throw new Error('当前浏览器不支持 PNG 转换，请右键另存图片');
      context.drawImage(image,0,0,width,height);
      canvas.toBlob(blob=>{
        if(!blob){toast('PNG 转换失败，请右键另存图片',true);return}
        const objectUrl=URL.createObjectURL(blob),anchor=document.createElement('a');
        anchor.href=objectUrl;anchor.download=pngName(url);
        document.body.appendChild(anchor);anchor.click();anchor.remove();
        setTimeout(()=>URL.revokeObjectURL(objectUrl),10000);
        toast(`已下载 ${anchor.download}（本地转换，无后端）`);
      },'image/png');
    }catch(error){toast(error.message||'PNG 转换失败，请右键另存图片',true)}
  };
  image.onerror=()=>toast('无法读取该图片，PNG 下载失败',true);
  image.src=url;
}

/* ---- large image viewer (shared with the workspace behaviour) ------------ */
function viewerBounds(){return $('.viewer-stage').getBoundingClientRect()}function clampView(){const i=$('#viewer img'),b=viewerBounds(),mx=Math.max(0,(i.naturalWidth*viewImage.scale-b.width)/2),my=Math.max(0,(i.naturalHeight*viewImage.scale-b.height)/2);viewImage.x=Math.max(-mx,Math.min(mx,viewImage.x));viewImage.y=Math.max(-my,Math.min(my,viewImage.y))}function applyView(){const i=$('#viewer img');if(!i.naturalWidth)return;clampView();i.style.width=`${i.naturalWidth}px`;i.style.height=`${i.naturalHeight}px`;i.style.transform=`translate(-50%,-50%) translate(${viewImage.x}px,${viewImage.y}px) scale(${viewImage.scale})`;$('#viewer-zoom').textContent=`${Math.round(viewImage.scale*100)}%`;$('.viewer-stage').classList.toggle('can-pan',i.naturalWidth*viewImage.scale>viewerBounds().width+1||i.naturalHeight*viewImage.scale>viewerBounds().height+1)}function fitViewer(){const i=$('#viewer img'),b=viewerBounds();if(!i.naturalWidth)return;viewImage.scale=Math.max(.1,Math.min(8,b.width/i.naturalWidth,b.height/i.naturalHeight));viewImage.x=viewImage.y=0;viewImage.fit=true;applyView()}function actualViewer(){viewImage.scale=1;viewImage.x=viewImage.y=0;viewImage.fit=false;applyView()}function zoomViewer(next,cx,cy){const b=viewerBounds(),old=viewImage.scale,z=Math.max(.1,Math.min(8,next)),x=(cx??b.left+b.width/2)-b.left-b.width/2,y=(cy??b.top+b.height/2)-b.top-b.height/2;viewImage.x=x-(x-viewImage.x)*z/old;viewImage.y=y-(y-viewImage.y)*z/old;viewImage.scale=z;viewImage.fit=false;applyView()}function viewer(n){if(!n?.src)return;viewImage.invoker=document.activeElement?.closest?.('[data-open],.image')||document.activeElement;Object.assign(viewImage,{scale:1,x:0,y:0,fit:true,pointer:null,src:n.src});$('#viewer-title').textContent=n.title||'生成结果大图';const i=$('#viewer img');i.onload=fitViewer;i.src=n.src;$('#viewer-original').href=n.src;$('#viewer-download').href=n.src;$('#viewer').showModal();if(i.complete&&i.naturalWidth)requestAnimationFrame(fitViewer);requestAnimationFrame(()=>$('#viewer-zoom-in').focus())}function closeViewer(){const d=$('#viewer');if(!d.open)return;d.close();const i=$('#viewer img');i.onload=null;i.removeAttribute('src');i.removeAttribute('style');$('.viewer-stage').classList.remove('can-pan','is-panning');const target=viewImage.invoker;Object.assign(viewImage,{scale:1,x:0,y:0,fit:true,pointer:null,invoker:null});requestAnimationFrame(()=>target?.isConnected&&target.focus?.({preventScroll:true}))}

/* ---- projects, sidebar and drawer -------------------------------------- */
function renderProjects(){const m=$('#project-list');m.innerHTML=store.projects.map(p=>`<div class="project-list-row ${p.id===state.id?'current':''}"><button class="project-list-item" aria-current="${p.id===state.id?'page':'false'}" data-project="${p.id}" title="${esc(p.name)}"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 9h8M8 13h5"/></svg><span>${esc(p.name)}</span></button><button class="project-rename-button" type="button" data-rename-project="${p.id}" aria-label="重命名 ${esc(p.name)}" title="重命名项目"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 17.5V20h2.5L17.7 8.8l-2.5-2.5L4 17.5Z"/><path d="m13.8 7.7 2.5 2.5"/></svg></button></div>`).join('');$('#project-name').textContent=state.name}
function switchProject(id){save();state=store.projects.find(p=>p.id===id);activePicker='';closeMenu();setDrawer(false);render();save()}
function worldCenter(){const r=$('#viewport').getBoundingClientRect();return{x:(r.width/2-state.view.x)/state.view.z,y:(r.height/2-state.view.y)/state.view.z}}
function restoreCenter(c){const r=$('#viewport').getBoundingClientRect();state.view.x=r.width/2-c.x*state.view.z;state.view.y=r.height/2-c.y*state.view.z;apply();links();minimap();save()}
function setCollapsed(on){const c=worldCenter();$('#workspace').classList.toggle('sidebar-collapsed',on);$('#sidebar-collapse').setAttribute('aria-expanded',String(!on));$('#sidebar-collapse').setAttribute('aria-label',on?'展开项目侧栏':'收起项目侧栏');$('#sidebar-collapse').title=on?'展开项目侧栏':'收起项目侧栏';localStorage.setItem('image-hub-demo-sidebar-collapsed',String(on));requestAnimationFrame(()=>restoreCenter(c))}
let drawerReturn=null;function setDrawer(on){if(!matchMedia('(max-width:768px)').matches)on=false;if(on)drawerReturn=document.activeElement;$('#workspace').classList.toggle('drawer-open',on);document.body.classList.toggle('drawer-open',on);$('#project-scrim').hidden=!on;$('#projects-button').setAttribute('aria-expanded',String(on));if(on)requestAnimationFrame(()=>$('#project-list .current')?.focus());else drawerReturn?.focus?.({preventScroll:true})}
function defaultProjectName(){const names=new Set(store.projects.map(p=>p.name)),base='未命名项目';if(!names.has(base))return base;let suffix=2;while(names.has(`${base} ${suffix}`))suffix++;return `${base} ${suffix}`}function createProject(){setDrawer(false);const p={id:uid('project'),version:SCHEMA_VERSION,name:defaultProjectName(),view:{x:0,y:0,z:1},selected:[],selectedLinks:[],clipboard:null,nodes:[]};store.projects.push(p);state=p;render();save()}function openRenameProjectDialog(projectId){setDrawer(false);const project=store.projects.find(p=>p.id===projectId);if(!project)return;const dialog=$('#rename-project'),input=$('#rename-project-name');dialog.dataset.projectId=project.id;input.value=project.name;dialog.showModal();requestAnimationFrame(()=>{input.focus();input.select()})}function closeRenameProjectDialog(){const dialog=$('#rename-project');if(dialog.open)dialog.close()}function clearRenameProjectDialog(){const dialog=$('#rename-project'),input=$('#rename-project-name');delete dialog.dataset.projectId;input.value='';input.setCustomValidity('')}

/* ---- canvas interaction -------------------------------------------------- */
function closeMenu(){$('#context-menu').hidden=true;context=null}
function contextItems(n,link){
  if(link)return[['移除连线','unlink']];
  if(!n)return[['上传图片','upload'],['新建生图','request'],['粘贴','paste',!state.clipboard],['适应内容','fit'],['快捷键','help']];
  if(isGeneration(n))return[['复制','copy'],['从画布移除','remove'],[n.expanded?'收起为结果简洁态':'展开编辑参数','toggle'],['从主图创建生图','derive',!primaryOf(n)?.artifactUrl]];
  return[['复制','copy'],['从画布移除','remove'],['打开大图','open'],['从此图创建生图','derive']];
}
/* Right-clicking a connection selects it first, so the menu and the keyboard
   share one target. Right-clicking blank canvas or a node keeps the existing
   items untouched. */
function openMenu(e,n,link){
  e.preventDefault();
  if(link&&!state.selectedLinks.includes(linkKey(link.source,link.target)))selectLink(link.source,link.target,false);
  context={id:n?.id||'',p:point(e.clientX,e.clientY),link:link||null};
  const m=$('#context-menu');
  m.innerHTML=contextItems(n,link).map(([l,a,d])=>`<button role="menuitem" data-action="${a}" ${d?'disabled':''}>${l}</button>`).join('');
  m.hidden=false;
  const r=m.getBoundingClientRect();
  m.style.left=`${Math.max(8,Math.min(e.clientX,innerWidth-r.width-8))}px`;
  m.style.top=`${Math.max(8,Math.min(e.clientY,innerHeight-r.height-8))}px`;
  $('button:not(:disabled)',m)?.focus();
}
function runAction(a){
  const n=node(context?.id),p=context?.p;
  if(a==='unlink')removeLinks([...state.selectedLinks]);
  else if(a==='upload'){$('#upload').dataset.x=p.x;$('#upload').dataset.y=p.y;$('#upload').click()}
  else if(a==='request')addGeneration(p.x,p.y);
  else if(a==='paste')paste(p);
  else if(a==='fit')fit();
  else if(a==='help')$('#shortcuts').hidden=false;
  else if(n){
    if(a==='copy'){if(!state.selected.includes(n.id))select(n.id,false);copy()}
    else if(a==='remove')remove(state.selected.includes(n.id)?state.selected:[n.id]);
    else if(a==='toggle')toggleGeneration(n);
    else if(a==='open')viewer({src:n.src,title:n.title||'参考图片'});
    else if(a==='derive'){
      const primary=primaryOf(n),ref=primary?refFor(n.id,primary.id):n.id;
      addGeneration(n.x+n.w+120,n.y,{inputs:[ref],parentGenerationId:primary?.generationId||'',prompt:n.prompt,profileId:n.profileId,ratio:primary?.parameters?.ratio||n.ratio,resolution:primary?.parameters?.resolution||n.resolution});
    }
  }
  closeMenu();
}
/* ---- history over unified nodes ----------------------------------------- */
function historyItems(){const items=[];state.nodes.filter(isGeneration).forEach(n=>nodeResults(n).forEach(result=>{if(result.status==='succeeded'&&result.artifactUrl)items.push({node:n,result})}));return items}
function history(){
  const items=historyItems();
  $('#history-grid').innerHTML=items.length?items.map(({node:n,result:r})=>`<article class="history-card"><img src="${esc(r.artifactUrl)}" alt="完整结果预览"><div><small>${esc(`${r.provider||''} · ${r.model||''} · ${r.parameters?.ratio||''} · ${r.parameters?.resolution||''}`)}</small><p>${esc(r.prompt||n.prompt)}</p><button data-locate="${esc(n.id)}" data-result-id="${esc(r.id)}">定位到画布</button><button class="primary" data-continue="${esc(n.id)}" data-result-id="${esc(r.id)}">继续创作</button></div></article>`).join(''):'<p>暂无记录</p>';
}

document.addEventListener('DOMContentLoaded',()=>{
  const vp=$('#viewport');
  render();
  const collapsed=localStorage.getItem('image-hub-demo-sidebar-collapsed')==='true';
  $('#workspace').classList.toggle('sidebar-collapsed',collapsed);
  $('#sidebar-collapse').setAttribute('aria-expanded',String(!collapsed));
  if(matchMedia('(max-width:420px)').matches){$('#minimap-map').hidden=true;$('#minimap-toggle').setAttribute('aria-expanded','false')}
  $('#add-request').onclick=()=>{const r=vp.getBoundingClientRect(),p=point(r.left+r.width/2-170,r.top+130);addGeneration(p.x,p.y)};
  $('#empty-request').onclick=()=>$('#add-request').click();
  $('#upload').onchange=e=>{const r=vp.getBoundingClientRect(),p=e.target.dataset.x?{x:+e.target.dataset.x,y:+e.target.dataset.y}:point(r.left+160,r.top+140);delete e.target.dataset.x;delete e.target.dataset.y;importExternalImages([...e.target.files],p);e.target.value=''};
  $('#fit').onclick=fit;$('#zoom-in').onclick=()=>zoom(state.view.z+.1,innerWidth/2,innerHeight/2);$('#zoom-out').onclick=()=>zoom(state.view.z-.1,innerWidth/2,innerHeight/2);$('#zoom').onclick=()=>zoom(1,innerWidth/2,innerHeight/2);
  vp.onwheel=e=>{e.preventDefault();zoom(state.view.z*(e.deltaY<0?1.08:.92),e.clientX,e.clientY)};
  vp.ondragover=e=>{e.preventDefault();if(e.dataTransfer)e.dataTransfer.dropEffect='copy';vp.classList.add('file-over')};
  vp.ondragleave=()=>vp.classList.remove('file-over');
  vp.ondrop=e=>{e.preventDefault();vp.classList.remove('file-over');importExternalImages(e.dataTransfer,point(e.clientX,e.clientY),{warnUrl:true})};
  const startConnect=(e,n,ref,selector)=>{e.preventDefault();e.stopPropagation();const start=portPoint(n.id,selector);if(!start)return;connecting={ref,nodeId:n.id,pointer:e.pointerId,start,current:start};document.body.classList.add('connecting');syncDragging();vp.setPointerCapture?.(e.pointerId);links()};
  vp.onpointerdown=e=>{
    closeMenu();
    /* A connection click is consumed before any pan or drag can start, so
       selecting a link never moves the canvas or a node. */
    const linkHit=e.target.closest?.('.link-hit');
    if(linkHit){e.preventDefault();if(e.button!==2)selectLink(linkHit.dataset.source,linkHit.dataset.target,e.shiftKey);return}
    const el=e.target.closest('.node'),n=el&&node(el.dataset.id);
    if(!n){if(e.button===0){e.preventDefault();pan={pointer:e.pointerId,sx:e.clientX,sy:e.clientY,x:state.view.x,y:state.view.y,moved:false};vp.setPointerCapture?.(e.pointerId);syncDragging()}return}
    const tilePort=e.target.closest('.tile-port');
    if(tilePort)return startConnect(e,n,tilePort.dataset.tileRef,'.tile-port');
    if(e.target.closest('.out'))return startConnect(e,n,n.id,'.out');
    if(e.target.closest('button,input,textarea,select,a,[draggable]'))return;
    pointerSelectionId=n.id;
    if(!state.selected.includes(n.id))select(n.id,e.shiftKey);
    /* Pointer capture is taken on the first real movement, never on
       pointerdown: capturing the viewport immediately retargets the following
       click/dblclick to the viewport, which silently killed result-image
       single click (expand) and double click (viewer). */
    drag={pointer:e.pointerId,sx:e.clientX,sy:e.clientY,origins:state.selected.map(id=>({id,x:node(id).x,y:node(id).y})),moved:false,captured:false};
    state.selected.forEach(id=>$(`[data-id="${id}"]`)?.classList.add('dragging'));
    syncDragging();
  };
  vp.onpointermove=e=>{
    lastCanvasPointer=point(e.clientX,e.clientY);
    if(connecting&&e.pointerId===connecting.pointer){connecting.current=point(e.clientX,e.clientY);links()}
    else if(drag&&e.pointerId===drag.pointer){
      const dx=(e.clientX-drag.sx)/state.view.z,dy=(e.clientY-drag.sy)/state.view.z;
      if(Math.hypot(dx,dy)>2){
        if(!drag.moved){drag.moved=true;if(!drag.captured){drag.captured=true;vp.setPointerCapture?.(e.pointerId)}}
        drag.origins.forEach(o=>{const n=node(o.id);if(!n)return;n.x=o.x+dx;n.y=o.y+dy;const el=$(`[data-id="${n.id}"]`);if(el){el.style.left=`${n.x}px`;el.style.top=`${n.y}px`}});links();
      }
    }
    else if(pan&&e.pointerId===pan.pointer){const dx=e.clientX-pan.sx,dy=e.clientY-pan.sy;if(Math.hypot(dx,dy)>4)pan.moved=true;state.view.x=pan.x+dx;state.view.y=pan.y+dy;apply();links();minimap()}
  };
  vp.onpointerup=e=>{
    if(connecting&&e.pointerId===connecting.pointer){
      const c=connecting;connecting=null;document.body.classList.remove('connecting');syncDragging();
      const under=document.elementFromPoint(e.clientX,e.clientY),dropTarget=under?.closest('.generation');
      if(dropTarget)addInput(dropTarget.dataset.id,c.ref);
      else if(under?.closest('#viewport')&&!under.closest('.node')){
        const source=resolveRef(c.ref),p=point(e.clientX,e.clientY);
        if(source)addGeneration(p.x,p.y,{inputs:[c.ref],parentGenerationId:source.result?.generationId||'',prompt:source.kind==='result'?source.node.prompt:'',profileId:source.kind==='result'?source.node.profileId:'',ratio:source.result?.parameters?.ratio,resolution:source.result?.parameters?.resolution});
        else links();
      } else links();
    }
    if(drag&&e.pointerId===drag.pointer){clearPointer(e.pointerId);save()}
    if(pan&&e.pointerId===pan.pointer){if(!pan.moved){state.selectedLinks=[];state.selected=[];render()}clearPointer(e.pointerId);save()}
    syncDragging();
  };
  vp.onpointercancel=e=>{if(connecting&&e.pointerId===connecting.pointer){connecting=null;document.body.classList.remove('connecting')}clearPointer(e.pointerId);syncDragging()};
  vp.onselectstart=e=>{if(drag||pan||connecting)e.preventDefault()};
  vp.oncontextmenu=e=>{
    const linkHit=e.target.closest?.('.link-hit');
    if(linkHit){openMenu(e,null,{source:linkHit.dataset.source,target:linkHit.dataset.target});return}
    const el=e.target.closest('.node');openMenu(e,el?node(el.dataset.id):null);
  };
  $('#nodes').ondblclick=e=>{
    clearTimeout(resultClickTimer);resultClickTimer=null;
    const el=e.target.closest('.node'),n=el&&node(el.dataset.id);if(!n)return;
    if(isImage(n)){if(n.src)viewer({src:n.src,title:n.title||'参考图片'});return}
    const tileEl=e.target.closest('.result-tile.ready');if(!tileEl)return;
    const result=batchResults(displayBatch(n)).find(r=>r.id===tileEl.dataset.result);
    if(result?.artifactUrl)viewer({src:result.artifactUrl,title:result.model||'生成结果'});
  };
  $('#nodes').oninput=e=>{
    const n=node(e.target.closest('.node')?.dataset.id);
    if(!n||e.target.dataset.field!=='prompt')return;
    n.prompt=e.target.value;syncDirty(n);
    const root=$(`[data-id="${n.id}"]`);
    if(root){const flag=$('.dirty-flag',root);if(flag)flag.hidden=!n.dirty}
    save();
  };
  $('#nodes').onchange=e=>{const n=node(e.target.closest('.node')?.dataset.id);if(!n)return;if(e.target.dataset.field==='count')n.count=clampCount(e.target.value);syncDirty(n);save()};
  $('#nodes').onclick=e=>{
    const el=e.target.closest('.node'),n=el&&node(el.dataset.id);if(!n)return;
    if(e.target.closest('[data-delete]')){remove([n.id]);return}
    if(e.target.closest('[data-reselect]')){reselectImage(n);return}
    if(e.target.closest('[data-retry]')){retry(n);return}
    if(!isGeneration(n)){
      if(e.target.closest('[data-open-image]')){viewer({src:n.src,title:n.title||'参考图片'});return}
      if(e.detail===0||pointerSelectionId!==n.id)select(n.id,e.shiftKey);
      pointerSelectionId='';
      return;
    }
    const batch=displayBatch(n);
    if(e.target.closest('[data-toggle]'))toggleGeneration(n);
    else if(e.target.closest('[data-generate]'))generate(n);
    else if(e.target.closest('[data-open-result]')){const p=primaryOf(n);if(p?.artifactUrl)viewer({src:p.artifactUrl,title:p.model||'生成结果'})}
    else if(e.target.closest('[data-download-result]')){const p=primaryOf(n);if(p?.artifactUrl)downloadPng(p.artifactUrl)}
    else if(e.target.closest('[data-sentiment]'))setSentiment(n,e.target.closest('[data-sentiment]').dataset.sentiment,e.target.closest('[data-sentiment]'));
    else if(e.target.closest('[data-remove]')){const ref=e.target.closest('[data-remove]').dataset.remove;n.inputs=n.inputs.filter(x=>x!==ref);syncDirty(n);render();save()}
    else if(e.target.closest('[data-model-trigger]')){activePicker=activePicker===`${n.id}:model`?'':`${n.id}:model`;render()}
    else if(e.target.closest('[data-image-trigger]')){activePicker=activePicker===`${n.id}:image`?'':`${n.id}:image`;render()}
    else if(e.target.closest('[data-close-picker]')){activePicker='';render()}
    else if(e.target.closest('[data-provider]')){n.pickerProvider=e.target.closest('[data-provider]').dataset.provider;render()}
    else if(e.target.closest('[data-profile]')){
      const target=profile(e.target.closest('[data-profile]').dataset.profile),old=`${n.ratio}|${n.resolution}`,combo=combos(target).find(x=>x.v===old)||combos(target)[0];
      if(!target.enabled)return;
      n.profileId=target.id;n.provider=target.provider;n.ratio=combo.r;n.resolution=combo.s;n.pickerProvider=target.provider;syncDirty(n);activePicker='';render();save();
    }
    else if(e.target.closest('[data-ratio]')){n.ratio=e.target.closest('[data-ratio]').dataset.ratio;syncDirty(n);activePicker='';render();save()}
    else if(e.target.closest('[data-resolution]')){n.resolution=e.target.closest('[data-resolution]').dataset.resolution;syncDirty(n);activePicker='';render();save()}
    else {
      const tileEl=e.target.closest('.result-tile'),result=tileEl&&batch?batchResults(batch).find(r=>r.id===tileEl.dataset.result):null;
      if(e.detail===0||pointerSelectionId!==n.id)select(n.id,e.shiftKey);
      pointerSelectionId='';
      if(!result)return;
      clearTimeout(resultClickTimer);
      resultClickTimer=setTimeout(()=>{resultClickTimer=null;activateResult(n,batch,result)},220);
    }
  };
  let moving=null;
  $('#nodes').ondragstart=e=>{const li=e.target.closest('[data-input]');if(!li){e.preventDefault();return}moving={owner:li.dataset.owner,ref:li.dataset.input}};
  $('#nodes').ondragover=e=>{if(moving&&e.target.closest('[data-input]'))e.preventDefault()};
  $('#nodes').ondrop=e=>{const li=e.target.closest('[data-input]');if(!li||!moving)return;e.preventDefault();const n=node(moving.owner);if(n){const list=n.inputs.filter(x=>x!==moving.ref),at=list.indexOf(li.dataset.input);list.splice(at<0?list.length:at,0,moving.ref);n.inputs=list;syncDirty(n)}moving=null;render();save()};
  $('#context-menu').onclick=e=>{const a=e.target.closest('[data-action]')?.dataset.action;if(a)runAction(a)};
  document.onpointerdown=e=>{if(!e.target.closest('#context-menu'))closeMenu();if(!e.target.closest('.visual-picker')&&activePicker){activePicker='';render()}};
  document.onkeydown=e=>{
    const editable=e.target.matches?.('input,textarea,select,[contenteditable="true"]'),meta=e.ctrlKey||e.metaKey,focused=vp===document.activeElement||!!document.activeElement?.closest?.('.node');
    if(e.key==='Escape'){
      const cleared=clearPointer();if(connecting){connecting=null;document.body.classList.remove('connecting')}if(cleared)syncDragging();
      if($('#rename-project').open){closeRenameProjectDialog();e.preventDefault();return}
      if($('#workspace').classList.contains('drawer-open'))setDrawer(false);
      else if(connecting||!$('#context-menu').hidden||activePicker||!$('#shortcuts').hidden){connecting=null;closeMenu();if(activePicker){activePicker='';render()}$('#shortcuts').hidden=true;links()}
      else if($('#viewer').open)closeViewer();
      else if(state.selectedLinks.length){state.selectedLinks=[];render()}
      else{state.selected=[];render()}
      return;
    }
    if($('#viewer').open){
      if(editable)return;
      if(['+','='].includes(e.key)){e.preventDefault();zoomViewer(viewImage.scale*1.2)}
      else if(['-','_'].includes(e.key)){e.preventDefault();zoomViewer(viewImage.scale/1.2)}
      else if(e.key==='0'){e.preventDefault();fitViewer()}
      else if(e.key==='1'){e.preventDefault();actualViewer()}
      return;
    }
    if(editable)return;
    if(e.key==='?'){e.preventDefault();$('#shortcuts').hidden=false}
    else if(meta&&e.key.toLowerCase()==='c'&&focused){e.preventDefault();copy()}
    else if(meta&&e.key.toLowerCase()==='a'&&focused){e.preventDefault();state.selectedLinks=[];state.selected=state.nodes.map(n=>n.id);select(state.nodes[0]?.id||'',false)}
    /* A selected connection wins over node selection; the two modes never
       coexist, so this single key stays unambiguous. Clicking a link leaves no
       focusable element behind, hence the explicit `selectedLinks` test. */
    else if(['Delete','Backspace'].includes(e.key)&&state.selectedLinks.length){e.preventDefault();removeLinks([...state.selectedLinks])}
    else if(['Delete','Backspace'].includes(e.key)&&focused&&state.selected.length){e.preventDefault();remove(state.selected)}
    else if(e.key==='0'&&focused)fit();
    else if(['+','='].includes(e.key)&&focused)zoom(state.view.z+.1,innerWidth/2,innerHeight/2);
    else if(['-','_'].includes(e.key)&&focused)zoom(state.view.z-.1,innerWidth/2,innerHeight/2);
  };
  document.addEventListener('paste',e=>{
    if(pasteEditable(e.target))return;
    const files=transferFiles(e.clipboardData);
    if(files.length){e.preventDefault();importExternalImages(files,lastCanvasPointer||worldCenter())}
    else if(hasUrlOnlyTransfer(e.clipboardData)){e.preventDefault();toast(urlOnlyMessage,true)}
    else if(state.clipboard?.ids.length){e.preventDefault();paste(lastCanvasPointer||worldCenter())}
  });
  $('#projects-button').onclick=()=>setDrawer(!$('#workspace').classList.contains('drawer-open'));
  $('#project-scrim').onclick=()=>setDrawer(false);
  $('#sidebar-collapse').onclick=()=>setCollapsed(!$('#workspace').classList.contains('sidebar-collapsed'));
  $$('[data-new]').forEach(b=>b.onclick=createProject);
  $('#project-list').onclick=e=>{
    const rename=e.target.closest('[data-rename-project]')?.dataset.renameProject;
    if(rename){openRenameProjectDialog(rename);return}
    const p=e.target.closest('[data-project]')?.dataset.project;
    if(p)switchProject(p);
  };
  $('#all-projects').onclick=()=>toast('Demo 中已显示所有项目');
  $$('[data-close-rename]').forEach(button=>button.onclick=closeRenameProjectDialog);
  $('#rename-project').onclick=e=>{if(e.target===e.currentTarget)closeRenameProjectDialog()};
  $('#rename-project').addEventListener('close',clearRenameProjectDialog);
  $('#rename-project-form').onsubmit=e=>{
    e.preventDefault();
    const input=$('#rename-project-name'),name=input.value.trim(),project=store.projects.find(p=>p.id===$('#rename-project').dataset.projectId);
    if(!name){input.setCustomValidity('项目名称不能为空');input.reportValidity();input.setCustomValidity('');return}
    if(store.projects.some(p=>p.id!==project?.id&&p.name===name)){toast('项目名称已存在',true);input.focus();input.select();return}
    if(project){project.name=name;closeRenameProjectDialog();render();save()}
  };
  /* One shared 画布 entry in the top bar replaces the scattered per-view return
     buttons: it closes history, account settings and administration, and it
     reports the current view through aria-current instead of doing nothing. */
  const demoViews={history:'#history',account:'#demo-account',admin:'#demo-admin'};
  function syncCanvasNav(){
    const button=$('#canvas-button');
    if(Object.values(demoViews).every(selector=>$(selector).hidden))button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');
  }
  function setDemoView(view){
    Object.entries(demoViews).forEach(([name,selector])=>{$(selector).hidden=name!==view});
    $('#demo-admin').classList.remove('nav-open');$('#demo-admin-menu').setAttribute('aria-expanded','false');
    syncCanvasNav();
  }
  $('#canvas-button').onclick=()=>setDemoView('canvas');
  $('#history-button').onclick=()=>{history();setDemoView('history')};
  $('#history-grid').onclick=e=>{
    const locate=e.target.dataset.locate,cont=e.target.dataset.continue,resultId=e.target.dataset.resultId;
    if(!locate&&!cont)return;
    const n=node(locate||cont);if(!n)return;
    setDemoView('canvas');
    if(locate){
      select(n.id,false);
      const v=vp.getBoundingClientRect();
      state.view={x:v.width/2-(n.x+n.w/2),y:v.height/2-(n.y+180),z:1};
      apply();links();minimap();
      if(batchResults(n.activeBatch).length>1&&n.primaryResultId!==resultId){n.primaryResultId=resultId;render();save()}
      requestAnimationFrame(()=>{const t=$(`[data-id="${n.id}"] [data-result="${resultId}"]`);if(!t)return;t.classList.add('attention');setTimeout(()=>t.classList.remove('attention'),1600)});
      return;
    }
    const source=nodeResults(n).find(r=>r.id===resultId);
    addGeneration(n.x+n.w+140,n.y,{inputs:[refFor(n.id,resultId)],parentGenerationId:source?.generationId||'',prompt:n.prompt,profileId:n.profileId,ratio:n.ratio,resolution:n.resolution,count:1});
    save();
  };
const setDemoAdminView=view=>{$$('[data-admin-panel]').forEach(panel=>panel.hidden=panel.dataset.adminPanel!==view);$$('[data-admin-view]').forEach(button=>button.classList.toggle('active',button.dataset.adminView===view));$('#demo-admin').classList.remove('nav-open');$('#demo-admin-menu').setAttribute('aria-expanded','false')};
const addDemoAudit=(action,summary)=>{$('#demo-audit-rows').insertAdjacentHTML('afterbegin',`<tr><td>刚刚</td><td>预览管理员</td><td>${esc(action)}</td><td>${esc(summary)}</td></tr>`)};
let confirmAction=null;const demoConfirm=(title,copy,action)=>{$('#demo-confirm-title').textContent=title;$('#demo-confirm-copy').textContent=copy;confirmAction=action;$('#demo-confirm').showModal()};
$('#demo-confirm').addEventListener('close',()=>{if($('#demo-confirm').returnValue==='confirm'&&confirmAction)confirmAction();confirmAction=null});
$('#account-button').onclick=()=>setDemoView('account');$('#demo-save-profile').onclick=()=>toast('虚构显示名称已在本地预览中更新');$('#demo-change-password').onclick=()=>{$$('#demo-account input[type="password"]').forEach(input=>input.value='');addDemoAudit('account.password_changed','用户自行修改密码（未保存明文）');toast('已模拟轮换登录态与 CSRF；未保存密码明文')};
$('#role-button').onclick=()=>{const ordinary=!$('#admin-button').hidden;$('#admin-button').hidden=ordinary;$('#role-button').textContent=ordinary?'普通用户':'管理员';$('#demo-account-role').textContent=ordinary?'普通用户':'管理员';$('#demo-account-username').textContent=ordinary?'demo.user':'admin.preview';if(ordinary){$('#demo-admin').hidden=true;syncCanvasNav()}toast('仅切换虚构 Demo 身份')};
$('#admin-button').onclick=()=>{setDemoAdminView('overview');setDemoView('admin')};$$('[data-admin-view]').forEach(button=>button.onclick=()=>setDemoAdminView(button.dataset.adminView));$('#demo-admin-menu').onclick=()=>{const open=!$('#demo-admin').classList.contains('nav-open');$('#demo-admin').classList.toggle('nav-open',open);$('#demo-admin-menu').setAttribute('aria-expanded',String(open))};
syncCanvasNav();
$('#demo-add-user').onclick=()=>{$('#demo-user-rows').insertAdjacentHTML('beforeend','<tr><td>new.preview</td><td>新建示例</td><td>设计研发部</td><td>普通用户</td><td>待修改初始密码</td><td><button data-demo-user-action="edit">编辑</button><button data-demo-user-action="reset">重置密码</button><button data-demo-user-action="toggle">停用</button></td></tr>');addDemoAudit('user.created','新建虚构账号 new.preview');toast('已新增虚构账号；初始密码未保存')};
$('#demo-user-rows').onclick=e=>{const action=e.target.dataset.demoUserAction,row=e.target.closest('tr');if(action==='edit'){row.children[1].textContent='已编辑示例';addDemoAudit('user.profile_updated','编辑虚构账号资料');toast('虚构资料已更新')}else if(action==='reset'){addDemoAudit('user.password_reset','管理员重置密码（未记录密码）');toast('已模拟重置；未保存密码明文')}else if(action==='toggle')demoConfirm('确认停用虚构账号？','项目、画布、历史和图片仍会保留。',()=>{row.children[4].textContent='停用';e.target.textContent='启用';addDemoAudit('user.disabled','停用虚构账号并撤销旧会话');toast('虚构账号已停用')})};
const clearDemoCredentialInputs=inputs=>inputs.forEach(input=>{input.value=''});const updateDemoCredentialStatus=(provider,configured)=>{const status=$(`#demo-${provider}-status`);status.textContent=configured?'已配置':'已清除';const inputs=provider==='libtv'?[$('#demo-libtv-token')]:[$('#demo-lovart-access-key'),$('#demo-lovart-secret-key')];inputs.forEach(input=>input.placeholder=configured?'已配置；留空保持不变':`输入 ${input.id.includes('access')?'Access Key':input.id.includes('secret')?'Secret Key':'LibTV Token'}`)};$('#demo-save-libtv-credentials').onclick=()=>{const input=$('#demo-libtv-token'),clear=$('#demo-clear-libtv');if(clear.checked){demoConfirm('确认清除 LibTV 凭据？','清除后仅模拟回退到环境配置或 CLI 登录。',()=>{clearDemoCredentialInputs([input]);clear.checked=false;updateDemoCredentialStatus('libtv',false);addDemoAudit('provider.libtv_credentials_cleared','LibTV 凭据已清除');toast('已模拟清除；未保存任何明文')});return}if(input.value.trim()){updateDemoCredentialStatus('libtv',true);addDemoAudit('provider.libtv_credentials_saved','LibTV 凭据已更新')}else addDemoAudit('provider.libtv_credentials_saved','LibTV 凭据保持');clearDemoCredentialInputs([input]);toast('仅模拟安全状态；未保存任何明文')};$('#demo-save-lovart-credentials').onclick=()=>{const inputs=[$('#demo-lovart-access-key'),$('#demo-lovart-secret-key')],clear=$('#demo-clear-lovart');if(clear.checked){demoConfirm('确认清除 Lovart 凭据？','清除后仅模拟回退到环境配置。',()=>{clearDemoCredentialInputs(inputs);clear.checked=false;updateDemoCredentialStatus('lovart',false);addDemoAudit('provider.lovart_credentials_cleared','Lovart 凭据已清除');toast('已模拟清除；未保存任何明文')});return}const filled=inputs.map(input=>Boolean(input.value.trim()));if(filled[0]!==filled[1]){toast('Access Key 与 Secret Key 必须成对填写',true);return}if(filled[0]){updateDemoCredentialStatus('lovart',true);addDemoAudit('provider.lovart_credentials_saved','Lovart 凭据已更新')}else addDemoAudit('provider.lovart_credentials_saved','Lovart 凭据保持');clearDemoCredentialInputs(inputs);toast('仅模拟安全状态；未保存任何明文')};const demoModelRow=`<fieldset class="demo-model-row"><legend>新增虚构模型</legend><button type="button" class="remove-model-row" aria-label="删除模型行">移除</button><input value="Preview Image" aria-label="显示名称"><input placeholder="保存时填写执行模型 ID" aria-label="执行模型 ID"><label><input type="checkbox" checked>1:1</label><select aria-label="分辨率"><option>1K</option><option selected>2K</option><option>4K</option></select></fieldset>`;$('#demo-add-model').onclick=()=>{$('#demo-model-rows').insertAdjacentHTML('beforeend',demoModelRow);$('#demo-model-rows .demo-model-row:last-child input')?.focus()};$('#demo-model-rows').onclick=e=>{const remove=e.target.closest('.remove-model-row');if(!remove)return;remove.closest('.demo-model-row').remove();addDemoAudit('provider.model_row_removed','移除一行虚构模型');if(!$('#demo-model-rows .demo-model-row')){$('#demo-model-rows').insertAdjacentHTML('beforeend',demoModelRow);addDemoAudit('provider.model_row_required','保留至少一行虚构模型')}toast('已移除该虚构模型行；未调用真实 Provider')};$('#demo-save-provider').onclick=()=>{addDemoAudit('provider.api_saved','保存虚构 API 配置；不含凭据与地址');toast('仅本地模拟保存，未连接真实 Provider')};$('#demo-recover').onclick=()=>{addDemoAudit('task.recovery_queried','查询虚构任务恢复状态');toast('仅模拟查询，未调用真实 Provider')};$('#demo-fail').onclick=()=>demoConfirm('确认收敛为失败？','不会重试或调用真实 Provider。',()=>{addDemoAudit('task.resolved_failed','将虚构任务收敛为失败');toast('虚构任务已收敛为失败')});
const vs=$('.viewer-stage');$('#viewer-close').onclick=closeViewer;$('#viewer-download').onclick=e=>{e.preventDefault();downloadPng(viewImage.src||'')};$('#viewer-zoom-out').onclick=()=>zoomViewer(viewImage.scale/1.2);$('#viewer-zoom-in').onclick=()=>zoomViewer(viewImage.scale*1.2);$('#viewer-fit').onclick=fitViewer;$('#viewer-actual').onclick=actualViewer;vs.onwheel=e=>{e.preventDefault();e.stopPropagation();zoomViewer(viewImage.scale*(e.deltaY<0?1.12:.89),e.clientX,e.clientY)};vs.onpointerdown=e=>{if(e.button!==0||!vs.classList.contains('can-pan'))return;e.preventDefault();e.stopPropagation();Object.assign(viewImage,{pointer:e.pointerId,sx:e.clientX,sy:e.clientY,ox:viewImage.x,oy:viewImage.y});vs.classList.add('is-panning');vs.setPointerCapture?.(e.pointerId)};vs.onpointermove=e=>{if(viewImage.pointer!==e.pointerId)return;viewImage.x=viewImage.ox+e.clientX-viewImage.sx;viewImage.y=viewImage.oy+e.clientY-viewImage.sy;applyView()};const endView=e=>{if(viewImage.pointer!==e.pointerId)return;viewImage.pointer=null;vs.classList.remove('is-panning');vs.releasePointerCapture?.(e.pointerId)};vs.onpointerup=endView;vs.onpointercancel=endView;$('#viewer').onclick=e=>{if(e.target===e.currentTarget)closeViewer()};$('[data-close]').onclick=()=>$('#shortcuts').hidden=true;$('#minimap-toggle').onclick=()=>{const s=$('#minimap-map');s.hidden=!s.hidden;$('#minimap-toggle').setAttribute('aria-expanded',String(!s.hidden));minimap()};$('#minimap-map').onpointerdown=e=>{miniDrag=e.pointerId;e.currentTarget.setPointerCapture?.(e.pointerId);miniPan(e)};$('#minimap-map').onpointermove=e=>{if(miniDrag===e.pointerId)miniPan(e)};$('#minimap-map').onpointerup=()=>{miniDrag=null;save()};window.onresize=()=>{if(!matchMedia('(max-width:768px)').matches)setDrawer(false);links();minimap();if($('#viewer').open)requestAnimationFrame(viewImage.fit?fitViewer:applyView)};
});
})();
