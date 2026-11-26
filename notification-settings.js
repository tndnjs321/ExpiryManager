/* Store-level push preferences; no scheduler is activated by this file. */
(()=>{
'use strict';
const el=id=>document.getElementById(id);
const defaults={enabled:false,notify_time:'09:00',timezone:'Asia/Seoul',notify_expired:true,notify_today:true,notify_upcoming:true,upcoming_days:2};
const fields=['storeNotifyEnabled','storeNotifyTime','storeNotifyExpired','storeNotifyToday','storeNotifySoon'];
let ctx=null,rev=0,ready=false,busy=false,loaded=null;
const table=c=>c.client.schema('bakery_dev').from('notification_settings');
const status=t=>{el('storeNotifyStatus').textContent=t};
function valid(c,s){return ctx===c&&rev===s}
function controls(){
 const locked=!ctx||!ready||busy;
 for(const id of fields)el(id).disabled=locked;
 el('storeNotifySave').disabled=locked;
 el('storeNotifyReload').disabled=!ctx||busy;
}
function display(row){
 el('storeNotifyEnabled').checked=row.enabled;
 el('storeNotifyTime').value=row.notify_time.slice(0,5);
 el('storeNotifyExpired').checked=row.notify_expired;
 el('storeNotifyToday').checked=row.notify_today;
 el('storeNotifySoon').checked=row.notify_upcoming;
}
async function load(){
 const c=ctx,s=rev;if(!c||busy)return;
 busy=true;ready=false;loaded=null;controls();status('매장 알림 설정 조회 중…');
 try{
  const {data,error}=await table(c).select('enabled,notify_time,timezone,notify_expired,notify_today,notify_upcoming,upcoming_days,updated_at').eq('store_id',c.storeId).maybeSingle();
  if(!valid(c,s))return;
  if(error)throw error;
  if(data&&(data.timezone!=='Asia/Seoul'||data.upcoming_days!==2))throw Error('기존 알림 조건이 다릅니다. 관리자에게 확인하세요.');
  loaded=data;
  display(data||defaults);ready=true;
  status(data?'저장된 매장 알림 설정입니다.':'저장된 설정이 없어 기본값을 표시합니다.');
 }catch(e){if(valid(c,s))status('설정 조회 실패: '+(e.message||'연결 오류'))}
 finally{if(valid(c,s)){busy=false;controls()}}
}
async function save(e){
 e.preventDefault();
 const c=ctx,s=rev;if(!c||!ready||busy)return;
 const v={enabled:el('storeNotifyEnabled').checked,notify_time:el('storeNotifyTime').value,timezone:'Asia/Seoul',notify_expired:el('storeNotifyExpired').checked,notify_today:el('storeNotifyToday').checked,notify_upcoming:el('storeNotifySoon').checked,upcoming_days:2};
 if(!/^([01][0-9]|2[0-3]):[0-5][0-9]$/.test(v.notify_time)){status('올바른 알림 시간을 입력하세요.');return}
 if(v.enabled&&!v.notify_expired&&!v.notify_today&&!v.notify_upcoming){status('알림 대상을 하나 이상 선택하세요.');return}
 busy=true;controls();status('설정 저장 중…');
 try{
  const payload={...v,updated_at:new Date().toISOString()};
  // Optimistic update: do not overwrite another administrator's concurrent change.
  const query=loaded
   ?table(c).update(payload).eq('store_id',c.storeId).eq('updated_at',loaded.updated_at)
   :table(c).insert({...payload,store_id:c.storeId});
  const {data,error}=await query.select('enabled,notify_time,timezone,notify_expired,notify_today,notify_upcoming,upcoming_days,updated_at').maybeSingle();
  if(!valid(c,s))return;
  if(error||!data){ready=false;status('설정을 저장하지 못했습니다. 다시 조회 후 재시도하세요.');return}
  loaded=data;display(data);status('매장 알림 설정 저장 완료');
 }catch(e){if(valid(c,s)){ready=false;status('설정 저장 실패. 다시 조회 후 재시도하세요.')}}
 finally{if(valid(c,s)){busy=false;controls()}}
}
window.expiryStoreNotify={
 attach(client,storeId,userId,role){
  if(!['owner','admin'].includes(role)){this.detach();return}
  if(ctx&&ctx.client===client&&ctx.storeId===storeId&&ctx.userId===userId&&ctx.role===role)return;
  ctx={client,storeId,userId,role};++rev;ready=false;busy=false;loaded=null;
  el('storeNotifyForm').hidden=false;display(defaults);controls();load();
 },
 detach(){ctx=null;++rev;ready=false;busy=false;loaded=null;el('storeNotifyForm').hidden=true;display(defaults);status('');controls()}
};
el('storeNotifyForm').addEventListener('submit',save);
el('storeNotifyReload').onclick=load;
controls();
})();
