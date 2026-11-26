/* Browser-side FCM registration. Supabase remains the only app authentication. */
(()=>{
'use strict';
const el=id=>document.getElementById(id);
const key='expiry-manager-dev-push-device';
let ctx=null, seq=0, working=false, sdkPromise=null, listener=null;
const status=t=>el('pushStatus').textContent=t;
function read(){try{return JSON.parse(localStorage.getItem(key)||'null')}catch{return null}}
function write(v){if(v)localStorage.setItem(key,JSON.stringify(v));else localStorage.removeItem(key)}
function own(c,r){return !!c&&!!r&&r.userId===c.userId&&r.storeId===c.storeId&&typeof r.id==='string'}
function active(c,s){return ctx===c&&seq===s}
function table(c){return c.client.schema('bakery_dev').from('push_devices')}
function controls(){el('pushEnable').disabled=!ctx||working;el('pushDisable').disabled=!ctx||working||!own(ctx,read())}
function supported(){return window.isSecureContext&&'Notification' in window&&'serviceWorker' in navigator&&'PushManager' in window}
async function sdk(){
 if(!sdkPromise)sdkPromise=(async()=>{
  const [app,m]=await Promise.all([import('https://www.gstatic.com/firebasejs/13.4.0/firebase-app.js'),import('https://www.gstatic.com/firebasejs/13.4.0/firebase-messaging.js')]);
  if(!await m.isSupported())throw Error('이 브라우저에서는 웹 푸시를 지원하지 않습니다.');
  const f=app.getApps().length?app.getApp():app.initializeApp(window.ExpiryFirebaseConfig);
  return {...m,messaging:m.getMessaging(f)};
 })().catch(e=>{sdkPromise=null;throw e});
 return sdkPromise;
}
async function worker(){
 await navigator.serviceWorker.register('./firebase-messaging-sw.js',{scope:'./'});
 const r=await Promise.race([navigator.serviceWorker.ready,new Promise((_,reject)=>setTimeout(()=>reject(Error('알림 서비스 준비 시간이 초과되었습니다.')),15000))]);
 if(!r.active||!r.active.scriptURL.endsWith('/firebase-messaging-sw.js'))throw Error('알림 서비스가 준비되지 않았습니다.');
 return r;
}
async function disableRow(c,r){
 const {error}=await table(c).update({enabled:false,updated_at:new Date().toISOString()}).eq('id',r.id).eq('user_id',c.userId).eq('store_id',c.storeId);
 if(error)throw error;
}
async function invalidate(){
 const f=await sdk();await f.deleteToken(f.messaging);
}
async function verify(c,s){
 const r=read();controls();
 if(!supported()){status('웹 푸시 미지원 환경입니다. 아이폰은 홈 화면에 추가한 앱에서 실행하세요.');return;}
 if(!own(c,r)){status('이 기기의 알림이 등록되지 않았습니다.');return;}
 try{
  const {data,error}=await table(c).select('id,enabled').eq('id',r.id).eq('user_id',c.userId).eq('store_id',c.storeId).maybeSingle();
  if(!active(c,s))return;
  if(error)throw error;
  status(data?.enabled&&Notification.permission==='granted'?'이 기기 알림 등록됨':'이 기기 알림이 꺼져 있습니다. 다시 등록할 수 있습니다.');
  controls();
 }catch{if(active(c,s))status('기기 등록 상태 조회 실패. 다시 시도하세요.')}
}
async function enable(){
 const c=ctx,s=seq;if(!c||working)return;
 working=true;controls();
 try{
  if(!supported())throw Error('이 환경은 웹 푸시를 지원하지 않습니다. 아이폰은 홈 화면 앱에서 실행하세요.');
  // The permission request must be initiated by the user's click.
  const permission=await Notification.requestPermission();
  if(permission!=='granted')throw Error('브라우저 알림 권한을 허용해 주세요.');
  if(!active(c,s))return;
  // Ensure the browser can track the device ID for later revocation.
  localStorage.setItem(key+'-check','1');localStorage.removeItem(key+'-check');
  status('알림 서비스 연결 중…');
  const f=await sdk(),registration=await worker(),previous=read();
  if(!active(c,s))return;
  // A shared browser must not reuse a previous account's FCM token.
  if(previous&&!own(c,previous))await f.deleteToken(f.messaging);
  if(own(c,previous))await disableRow(c,previous);
  const token=await f.getToken(f.messaging,{vapidKey:window.ExpiryVapidKey,serviceWorkerRegistration:registration});
  if(!token)throw Error('알림 토큰을 발급받지 못했습니다.');
  if(!active(c,s))return;
  const {data,error}=await table(c).upsert({store_id:c.storeId,user_id:c.userId,fcm_token:token,device_name:/iPhone|iPad/.test(navigator.userAgent)?'iOS 웹 앱':/Android/.test(navigator.userAgent)?'Android 웹 앱':'웹 브라우저',enabled:true,updated_at:new Date().toISOString()},{onConflict:'fcm_token'}).select('id').single();
  if(error)throw error;
  try{write({id:data.id,storeId:c.storeId,userId:c.userId});}catch(e){await disableRow(c,{id:data.id});throw e;}
  if(!active(c,s))return;
  if(!listener)listener=f.onMessage(f.messaging,()=>{if(ctx)status('유통기한 알림이 도착했습니다. 앱의 상품 목록을 확인하세요.');});
  status('이 기기 알림 등록 완료');
 }catch(e){if(active(c,s))status('등록 실패: '+(e.code||e.message||'연결 오류'))}
 finally{working=false;controls()}
}
async function disable(){
 const c=ctx,s=seq,r=read();if(!c||working||!own(c,r))return false;
 working=true;controls();
 try{
  await disableRow(c,r);
  try{await invalidate()}catch{/* Server-side disabled state prevents later scheduled sends. */}
  write(null);
  if(active(c,s))status('이 기기 알림 해제 완료');
  return true;
 }catch{if(active(c,s))status('알림 해제 실패. 연결 확인 후 다시 시도하세요.');return false}
 finally{working=false;controls()}
}
window.expiryPush={
 attach(client,storeId,userId){
  if(ctx&&ctx.client===client&&ctx.storeId===storeId&&ctx.userId===userId)return;
  ctx={client,storeId,userId};const s=++seq;verify(ctx,s);
 },
 detach(){ctx=null;++seq;status('');controls()},
 async beforeLogout(){
  if(working){status('알림 설정 처리 중입니다. 완료 후 로그아웃하세요.');return false;}
  if(!own(ctx,read()))return true;
  if(await disable())return true;
  // If DB revocation fails, still try to stop local push; do not trap user in session.
  try{await invalidate();write(null);return true;}catch{
   try{const r=await navigator.serviceWorker.getRegistration('./');const sub=await r?.pushManager.getSubscription();if(sub)await sub.unsubscribe();write(null);}catch{}
   return window.confirm('이 기기 알림 해제를 확인하지 못했습니다. 로그아웃하시겠습니까? 브라우저 알림 권한도 해제해 주세요.');
  }
 }
};
el('pushEnable').onclick=enable;el('pushDisable').onclick=disable;controls();
})();
