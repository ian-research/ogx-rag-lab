// Execute the real UI script in a minimal DOM with all network calls mocked.
const {test}=require('node:test');const assert=require('node:assert/strict');
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
function setup(){
 class El {constructor(){this.children=[];this.value='';this.textContent='';this.listeners={};this.classList={toggle(){}};}append(...v){this.children.push(...v);}replaceChildren(...v){this.children=v;}add(v){this.children.push(v);}addEventListener(k,f){this.listeners[k]=f;}reportValidity(){return true;}focus(){}}
 const els=new Map(),get=id=>{if(!els.has(id))els.set(id,new El());return els.get(id);};
 const context=vm.createContext({document:{getElementById:get,createElement:()=>new El()},Option:function(t,v){this.text=t;this.value=v;},setInterval(){},fetch:async url=>({ok:true,json:async()=>url==='/api/health'?{worker_alive:true}:{versions:[]}}),FormData:class{constructor(form){this.d=new Map(Object.entries(form.fields));}get(k){return this.d.get(k);}set(k,v){this.d.set(k,v);}delete(k){this.d.delete(k);}},confirm:()=>true,prompt:()=>null});
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../ui.js'),'utf8'),context);
 return {get,run:code=>vm.runInContext(code,context)};
}
test('P4 only published ready rows offer question action',async()=>{
 const {get,run}=setup();await new Promise(r=>setImmediate(r));
 run(`versions=['draft','withdrawn','published'].map((s,i)=>({version_id:String(i),publication_status:s,job_status:i===0?'failed':'ready',policy_id:'p',scope:'all',version_label:'v'+i}));render()`);
 const buttons=get('versions').children.map(row=>row.children[4].children.map(b=>b.textContent));
 assert(!buttons[0].includes('用于问答'));assert(!buttons[1].includes('用于问答'));assert(buttons[2].includes('用于问答'));
});
test('P5 both typing and file change clear previous upload notice',()=>{
 const {get}=setup();for(const event of ['input','change']){get('notice').textContent='上次提交';get('uploadForm').listeners[event]();assert.equal(get('notice').textContent,'');}
});
test('P3 selected DOC is rejected in submit before upload request',async()=>{
 const {get}=setup();get('uploadForm').fields={policy_id:'p',policy_title:'title',version_label:'v1',scope:'all',valid_from:'2026-01-01'};
 get('file').files=[{name:'legacy.DOC',size:10}];await get('uploadForm').onsubmit({preventDefault(){}});
 assert.match(get('notice').textContent,/另存为 DOCX/);assert.notEqual(get('upload').disabled,true);
});
