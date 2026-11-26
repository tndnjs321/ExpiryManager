/* Production web push only. No page, session or inventory cache. */
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  event.waitUntil((async()=>{
    const url=new URL('./',self.registration.scope).href;
    const windows=await self.clients.matchAll({type:'window',includeUncontrolled:true});
    for(const client of windows){
      if(client.url.startsWith(self.registration.scope)){
        await client.focus();
        return;
      }
    }
    await self.clients.openWindow(url);
  })());
});
importScripts('./firebase-config.js');
importScripts('https://www.gstatic.com/firebasejs/13.4.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/13.4.0/firebase-messaging-compat.js');
firebase.initializeApp(self.ExpiryFirebaseConfig);
const messaging=firebase.messaging();
messaging.onBackgroundMessage(payload=>{
  // Notification payloads are rendered by FCM itself; avoid a second notification.
  if(payload.notification)return;
  // Do not display store or product details on a locked/shared device.
  return self.registration.showNotification('유통기한 관리',{
    body:'확인할 유통기한 알림이 있습니다. 앱에서 확인하세요.',
    icon:'./icon.svg',
    tag:'expiry-manager-prod',
    data:{url:new URL('./',self.registration.scope).href}
  });
});
