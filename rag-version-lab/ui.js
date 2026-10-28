'use strict';
const $ = id => document.getElementById(id);
const jobNames = {queued:'排队中', parsing:'解析中', chunking:'分块中', embedding:'向量化 / 入库', ready:'入库完成', failed:'失败'};
const pubNames = {draft:'未发布', published:'已发布', withdrawn:'已撤回'};
let versions = [], refreshing = false;
const busy = new Set();
async function api(url, options) {
  const r = await fetch(url, options);
  const data = await r.json();
  if (!r.ok) {
    let detail = data.detail;
    if (Array.isArray(detail)) detail = detail.map(e => e.loc.join('.') + '：' + e.msg).join('\n');
    throw Error(typeof detail === 'string' ? detail : JSON.stringify(data));
  }
  return data;
}
function show(id, text, error=false) { $(id).textContent = text; $(id).classList.toggle('error', error); }
function node(tag, text) {const e=document.createElement(tag); e.textContent=text; return e;}
function updateReplacements() {
  const select=$('supersedes'), old=select.value;
  const choices=versions.filter(v => v.policy_id===$('policy').value.trim() && v.scope===$('scope').value.trim() && v.publication_status==='published');
  select.replaceChildren(new Option('不指定替代关系', ''));
  choices.forEach(v => select.add(new Option(`${v.version_label} · ${v.valid_from} → ${v.valid_to || '无预设结束日期'} · ${v.filename}`, v.version_id)));
  if (choices.some(v=>v.version_id===old)) select.value=old;
  $('replaceHint').textContent=choices.length ? '只列出同文档、同范围的已发布版本。发布新版时将截断被替代版的有效期；未指定关系且日期重叠会被拒绝发布。' : '没有匹配的已发布版本。首次上传可留空；请核对文档标识和范围。';
}
function render() {
  $('versions').replaceChildren();
  if (!versions.length) {const tr=node('tr',''),td=node('td','还没有文档版本，请先上传。');td.colSpan=5;tr.append(td);$('versions').append(tr);}
  for (const v of versions) {
    const tr=document.createElement('tr'), cells=Array.from({length:5},()=>document.createElement('td'));
    cells.forEach(c=>tr.append(c));
    cells[0].append(node('strong',v.policy_title || v.policy_id),node('small',v.policy_id+' · '+v.version_label));
    cells[1].append(node('div',v.filename),node('small','范围：'+v.scope));
    cells[2].append(node('div',v.valid_from+' 起'),node('small',v.valid_to?'至 '+v.valid_to+'（不含）':'无预设结束日期'));
    const badge=node('span',jobNames[v.job_status]||v.job_status);badge.className='badge'+(v.job_status==='ready'?' ready':'');
    cells[3].append(badge,node('small',pubNames[v.publication_status]||v.publication_status));
    if(v.total)cells[3].append(node('small',`${v.done}/${v.total} 个片段`));
    const add=(label,handler)=>{const b=node('button',label);b.type='button';b.disabled=busy.has(v.version_id);b.onclick=async()=>{try{await handler();}catch(e){show('actionNotice',e.message,true);}};cells[4].append(b);};
    add('详情',()=>{$('detail').textContent=JSON.stringify(v,null,2);$('detail').parentElement.open=true;});
    if(v.publication_status==='draft' && v.job_status==='ready')add('发布',()=>mutate(v,'publish'));
    if(v.publication_status==='draft' && v.job_status==='failed')add('重试入库',()=>mutate(v,'retry'));
    if(v.publication_status==='published')add('撤回',()=>mutate(v,'withdraw'));
    if(v.publication_status==='published' && v.job_status==='ready')add('用于问答',()=>{$('askPolicy').value=v.policy_id;$('askScope').value=v.scope;$('question').focus();show('answer','已选择文档与范围。查询日期仍决定适用版本，并不强制使用此行版本。');});
    $('versions').append(tr);
  }
  for (const [id, key] of [['policies','policy_id'],['scopes','scope']]) {
    $(id).replaceChildren(...[...new Set(versions.map(v=>v[key]))].map(v=>new Option(v,v)));
  }
  updateReplacements();
}
async function refresh() {
  if(refreshing)return; refreshing=true;
  try {
    const results=await Promise.allSettled([api('/api/health'),api('/api/versions')]);
    if(results[0].status==='fulfilled')show('health',results[0].value.worker_alive?'后台处理服务：运行中':'后台处理服务：未检测到心跳，任务需要等待 Worker 恢复',!results[0].value.worker_alive);
    else show('health','服务连接失败：'+results[0].reason.message,true);
    if(results[1].status==='fulfilled'){versions=results[1].value.versions;render();}
    else show('actionNotice','列表刷新失败：'+results[1].reason.message,true);
  } finally {refreshing=false;}
}
async function mutate(v,action) {
  if(busy.has(v.version_id))return;
  let suffix=action;
  if(action==='publish' && !confirm(`发布 ${v.policy_id} / ${v.version_label}？\n生效日期：${v.valid_from}${v.supersedes?'\n发布将更新被替代版本的有效期。':''}`))return;
  if(action==='withdraw') {
    const reason=prompt('撤回后此版本不再参与任何日期的检索；有可恢复记录且无冲突时会恢复旧版有效期。请填写撤回原因：');
    if(reason===null)return;
    if(!reason.trim()){show('actionNotice','撤回原因不能为空。',true);return;}
    suffix+='?reason='+encodeURIComponent(reason.trim());
  }
  busy.add(v.version_id);render();
  try{const result=await api('/api/versions/'+encodeURIComponent(v.version_id)+'/'+suffix,{method:'POST'});show('actionNotice',`${v.version_label}：${{publish:'发布成功；是否适用仍取决于查询日期。',withdraw:'已撤回。',retry:'已重新排队。'}[action]}`);if(action==='withdraw'){const r=result.restoration;const reasons={legacy_snapshot_missing:'旧记录没有截断前的有效期，无法自动恢复。',no_predecessor:'此版本没有前一版本。',predecessor_not_published:'前一版本已撤回或不存在。',predecessor_changed:'前一版本已被后续操作修改。',overlap:'恢复会与其他已发布版本重叠。'};show('actionNotice',r?.status==='restored'?'已撤回，并恢复前一版本的有效期至 '+(r.valid_to||'无预设结束日期')+'。':'已撤回，未自动恢复旧版：'+(reasons[r?.reason]||'请核对发布记录。')+' 请检查是否存在适用空档。');}await refresh();}
  catch(e){show('actionNotice',e.message,true);}
  finally{busy.delete(v.version_id);render();}
}
$('policy').addEventListener('input',updateReplacements);
$('scope').addEventListener('input',updateReplacements);
$('refresh').onclick=refresh;
$('uploadForm').addEventListener('input',()=>show('notice',''));
$('uploadForm').addEventListener('change',()=>show('notice',''));
$('uploadForm').onsubmit=async event=>{
  event.preventDefault();const form=$('uploadForm');if(!form.reportValidity())return;
  const data=new FormData(form),file=$('file').files[0];
  for(const key of ['policy_id','policy_title','version_label','scope'])data.set(key,String(data.get(key)).trim());
  if(['policy_id','version_label','scope'].some(k=>!data.get(k))){show('notice','文档标识、版本标签和范围不能为空。',true);return;}
  if(file && /\.doc$/i.test(file.name)){show('notice','暂不支持旧版 DOC，请先另存为 DOCX 后上传',true);return;}
  if(!file || !file.size || file.size>50*1024*1024){show('notice','请选择非空且不超过 50 MiB 的文件。',true);return;}
  if(data.get('valid_to') && data.get('valid_to')<=data.get('valid_from')){show('notice','失效日期必须晚于生效日期。',true);return;}
  for(const key of ['valid_to','supersedes'])if(!data.get(key))data.delete(key);
  $('upload').disabled=true;show('notice','正在上传…');
  try{const v=await api('/api/versions',{method:'POST',body:data});show('notice',`${v.filename} / ${v.version_label} 已提交后台处理。入库完成后，请在列表中点击“发布”。`);$('file').value='';$('askPolicy').value=v.policy_id;$('askScope').value=v.scope;await refresh();}
  catch(e){show('notice',e.message,true);}finally{$('upload').disabled=false;}
};
$('askForm').onsubmit=async event=>{
  event.preventDefault();if(!$('askForm').reportValidity())return;
  const body={question:$('question').value.trim(),scope:$('askScope').value.trim()};
  if(!body.question || !body.scope){show('answer','问题和适用范围不能为空。',true);return;}
  if($('askPolicy').value.trim())body.policy_id=$('askPolicy').value.trim();
  if($('asOf').value)body.as_of=$('asOf').value;
  $('ask').disabled=true;$('candidates').replaceChildren();show('answer','正在选版、检索并生成回答…');
  try{
    const data=await api('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const lines=[data.answer,`\n查询日期：${data.as_of} · 范围：${data.scope}`];
    if(data.version)lines.push(`适用文档：${data.version.policy_id} / ${data.version.version_label}`);
    for(const s of data.sources||[])lines.push(`\n[${s.number}] ${s.source} · ${s.version_label} · ${{ocr:'OCR识别',table:'表格',text:'文本'}[s.kind]||'来源类型未知'}${s.page?' · 页 '+s.page:''}\n${s.text}`);
    if(data.finish_reason==='length')lines.push('\n回答可能被截断。');
    show('answer',lines.join('\n'));
    if(data.status==='needs_selection')for(const id of new Set((data.candidates||[]).map(v=>v.policy_id))){const b=node('button','选择 '+id);b.type='button';b.onclick=()=>{$('askPolicy').value=id;$('askForm').requestSubmit();};$('candidates').append(b);}
  }catch(e){show('answer',e.message,true);}finally{$('ask').disabled=false;}
};
refresh();setInterval(refresh,3000);
