/* Store-level multi-schedule settings. Scheduling and sending are separate. */
(()=>{
'use strict';
const el=id=>document.getElementById(id);
const status=t=>{el('storeNotifyStatus').textContent=t};
const categories=[['notify_expired','기한 경과'],['notify_today','오늘까지'],['notify_upcoming','2일 이내 (내일~모레)']];
let ctx=null,rev=0,busy=false,ready=false,expected=null,drafts=[],dirty=false;
const rpc=(c,name,args)=>c.client.schema('bakery').rpc(name,args);
const valid=(c,s)=>ctx===c&&rev===s;
const node=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
function controls(){
 const locked=!ctx||!ready||busy;
 el('storeNotifyEnabled').disabled=locked;
 el('storeNotifyAdd').disabled=locked||drafts.length>=10;
 el('storeNotifySave').disabled=locked;
 el('storeNotifyReload').disabled=!ctx||busy;
 for(const n of el('storeNotifySchedules').querySelectorAll('input,button'))n.disabled=locked;
 el('storeNotifyCount').textContent='등록된 알림 일정 '+drafts.length+' / 10개 · 한국시간';
}
function changed(){dirty=true;status('변경한 설정을 저장해 주세요.')}
function checkbox(text,checked,onchange){
 const label=node('label');label.className='backup-check';
 const input=node('input');input.type='checkbox';input.checked=checked;
 input.addEventListener('change',()=>{onchange(input.checked);changed()});
 label.append(input,document.createTextNode(text));return label;
}
function render(){
 const list=el('storeNotifySchedules');list.replaceChildren();
 if(!drafts.length){const p=node('p','등록된 알림 일정이 없습니다. 알림 일정을 추가하세요.');p.className='muted';list.append(p)}
 drafts.forEach((d,i)=>{
  const card=node('div');card.className='notify-schedule';
  const row=node('div');row.className='row';
  const heading=node('strong','알림 '+(i+1));
  const remove=node('button','삭제');remove.type='button';
  remove.setAttribute('aria-label','알림 '+(i+1)+' 삭제');
  remove.onclick=()=>{if(!ready||busy)return;drafts.splice(i,1);changed();render()};
  row.append(heading,remove);card.append(row);
  card.append(checkbox('이 일정 사용',d.enabled,v=>{d.enabled=v}));
  const label=node('label','알림 시간');label.className='notify-time-label';
  const time=node('input');time.type='time';time.step='60';time.required=true;time.value=d.notify_time;
  time.addEventListener('input',()=>{d.notify_time=time.value;changed()});
  time.addEventListener('change',()=>{d.notify_time=time.value;changed()});
  label.append(time);card.append(label);
  const caption=node('div','알림 대상');caption.className='muted';card.append(caption);
  for(const [key,text] of categories)card.append(checkbox(text,d[key],v=>{d[key]=v}));
  list.append(card);
 });
 controls();
}
function apply(data){
 if(!data||!Array.isArray(data.schedules)||data.schedules.length>10)throw Error('알림 설정 응답이 올바르지 않습니다.');
 if(data.settings&&(data.settings.timezone!=='Asia/Seoul'||data.settings.upcoming_days!==2))throw Error('기존 알림 조건이 다릅니다. 관리자에게 확인하세요.');
 if(data.schedules.some(d=>!d.id||typeof d.notify_time!=='string'))throw Error('알림 일정 응답이 올바르지 않습니다.');
 expected=data;
 el('storeNotifyEnabled').checked=!!data.settings?.enabled;
 drafts=data.schedules.map(d=>({id:d.id,enabled:d.enabled,notify_time:d.notify_time.slice(0,5),notify_expired:d.notify_expired,notify_today:d.notify_today,notify_upcoming:d.notify_upcoming}));
 dirty=false;ready=true;render();
}
async function load(){
 const c=ctx,s=rev;if(!c||busy)return;
 busy=true;ready=false;controls();status('매장 알림 설정 조회 중…');
 try{
  const {data,error}=await rpc(c,'get_notification_config',{p_store_id:c.storeId});
  if(!valid(c,s))return;
  if(error)throw error;
  apply(data);
  status(data.settings?'저장된 매장 알림 설정입니다.':'알림 일정을 추가하여 설정을 저장하세요.');
 }catch(e){if(valid(c,s)){ready=false;status('설정 조회 실패: '+(e.message||'연결 오류'))}}
 finally{if(valid(c,s)){busy=false;controls()}}
}
function add(){
 if(!ctx||!ready||busy||drafts.length>=10)return;
 const used=new Set(drafts.map(d=>d.notify_time));
 let time=['09:00','12:00','14:00','18:00','20:00'].find(t=>!used.has(t));
 if(!time){for(let h=0;h<24;h++){const t=String(h).padStart(2,'0')+':00';if(!used.has(t)){time=t;break}}}
 drafts.push({id:null,enabled:true,notify_time:time,notify_expired:true,notify_today:true,notify_upcoming:true});
 changed();render();
}
async function save(e){
 e.preventDefault();const c=ctx,s=rev;if(!c||!ready||busy)return;
 const enabled=el('storeNotifyEnabled').checked;
 if(drafts.length>10){status('알림 일정은 최대 10개까지 등록할 수 있습니다.');return}
 const times=new Set();
 for(const d of drafts){
  if(!/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(d.notify_time)){status('올바른 알림 시간을 입력하세요.');return}
  if(times.has(d.notify_time)){status('동일한 알림 시간은 중복 등록할 수 없습니다.');return}
  times.add(d.notify_time);
  if(d.enabled&&!categories.some(([key])=>d[key])){status('사용 중인 일정은 알림 대상을 하나 이상 선택하세요.');return}
 }
 const payload=drafts.map((d,i)=>({...d,slot_no:i+1}));
 busy=true;controls();status('설정 저장 중…');
 try{
  const {data,error}=await rpc(c,'save_notification_config',{p_store_id:c.storeId,p_enabled:enabled,p_schedules:payload,p_expected:expected});
  if(!valid(c,s))return;
  if(error)throw error;
  apply(data);
  status('매장 알림 설정 저장 완료'+(enabled&&!drafts.some(d=>d.enabled)?' · 사용 중인 일정이 없습니다.':''));
 }catch(e){if(valid(c,s)){ready=false;status('설정 저장 실패: '+(e.message||'연결 오류')+' · 다시 조회 후 재시도하세요.');}}
 finally{if(valid(c,s)){busy=false;controls()}}
}
window.expiryStoreNotify={
 attach(client,storeId,userId,role){
  if(!['owner','admin'].includes(role)){this.detach();return}
  if(ctx&&ctx.client===client&&ctx.storeId===storeId&&ctx.userId===userId&&ctx.role===role)return;
  ctx={client,storeId,userId,role};++rev;busy=false;ready=false;expected=null;drafts=[];dirty=false;
  el('storeNotifyEnabled').checked=false;el('storeNotifyForm').hidden=false;render();load();
 },
 detach(){ctx=null;++rev;busy=false;ready=false;expected=null;drafts=[];dirty=false;el('storeNotifyEnabled').checked=false;el('storeNotifyForm').hidden=true;status('');render()}
};
el('storeNotifyEnabled').addEventListener('change',changed);
el('storeNotifyAdd').onclick=add;
el('storeNotifyForm').addEventListener('submit',save);
el('storeNotifyReload').onclick=()=>{if(!dirty||window.confirm('저장하지 않은 변경사항을 취소하고 다시 조회하시겠습니까?'))load()};
controls();
})();
