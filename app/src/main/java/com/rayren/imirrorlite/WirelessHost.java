package com.rayren.imirrorlite;

import android.content.Context;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.net.wifi.WifiManager;
import org.json.JSONObject;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Android owns multicast permissions, Bonjour advertising and discovery. */
final class WirelessHost implements AutoCloseable {
    interface Listener {
        void device(String json);
        void status(String kind, String message);
    }
    private final NsdManager nsd;
    private final Listener listener;
    private final WifiManager.MulticastLock multicast;
    private final List<NsdManager.RegistrationListener> registrations = new ArrayList<>();
    private final List<NsdManager.DiscoveryListener> discoveries = new ArrayList<>();
    private final Queue<NsdServiceInfo> pending = new ArrayDeque<>();
    private final Map<String,NsdServiceInfo> resolved = new HashMap<>();
    private final Map<String,Long> revisions = new HashMap<>();
    private long nextRevision;
    private final ExecutorService callbacks = Executors.newSingleThreadExecutor();
    private boolean closed, resolving;

    private static String key(NsdServiceInfo info) {
        String type=info.getServiceType();
        return (type==null?"":type.replaceAll("\\.+$", ""))+"|"+info.getServiceName();
    }

    WirelessHost(Context context, Listener listener) {
        this.listener=listener;
        nsd=(NsdManager)context.getSystemService(Context.NSD_SERVICE);
        WifiManager wifi=(WifiManager)context.getApplicationContext().getSystemService(Context.WIFI_SERVICE);
        multicast=wifi.createMulticastLock("imirror-airplay");
        multicast.setReferenceCounted(false);
        multicast.acquire();
    }

    synchronized void start(int port, String name, String rawTxt) throws Exception {
        if(closed) return;
        JSONObject txt=new JSONObject(rawTxt);
        NsdServiceInfo airplay=new NsdServiceInfo();
        airplay.setServiceName(name); airplay.setServiceType("_airplay._tcp."); airplay.setPort(port);
        Iterator<String> keys=txt.keys();
        while(keys.hasNext()) { String key=keys.next(); airplay.setAttribute(key,txt.getString(key)); }
        register(airplay);
        NsdServiceInfo raop=new NsdServiceInfo();
        raop.setServiceName(txt.getString("deviceid").replace(":","")+"@"+name);
        raop.setServiceType("_raop._tcp."); raop.setPort(port);
        String[][] attributes={{"txtvers","1"},{"ch","2"},{"cn","0,1,2,3"},{"et","0,3,5"},
            {"ft",txt.getString("features")},{"sf","0x4"},{"tp","UDP"},{"vn","65537"},
            {"am","AppleTV3,2"},{"vs","220.68"},{"vv","2"},{"pk",txt.getString("pk")},
            {"sr","44100"},{"ss","16"},{"da","true"},{"sv","false"}};
        for(String[] entry:attributes) raop.setAttribute(entry[0],entry[1]);
        register(raop);
        discover("_remotepairing._tcp.");
        discover("_remotepairing-manual-pairing._tcp.");
    }

    private void register(NsdServiceInfo info) {
        NsdManager.RegistrationListener registration=new NsdManager.RegistrationListener() {
            public void onServiceRegistered(NsdServiceInfo service) {
                synchronized(WirelessHost.this) {
                    if(closed) {
                        try { nsd.unregisterService(this); } catch(IllegalArgumentException ignored) {}
                        return;
                    }
                }
                // Android 12's registration callback may contain only the
                // allocated name; use the requested type, which we own.
                if(info.getServiceType().startsWith("_airplay"))
                    listener.status("video","在 iPhone 屏幕镜像中选择「"+service.getServiceName()+"」");
            }
            public void onRegistrationFailed(NsdServiceInfo service,int error) {
                listener.status("error","无线投屏广播失败（"+error+"），请检查 Wi-Fi 后重试");
            }
            public void onServiceUnregistered(NsdServiceInfo service) {}
            public void onUnregistrationFailed(NsdServiceInfo service,int error) {}
        };
        registrations.add(registration);
        nsd.registerService(info,NsdManager.PROTOCOL_DNS_SD,registration);
    }

    private void discover(String type) {
        NsdManager.DiscoveryListener discovery=new NsdManager.DiscoveryListener() {
            public void onDiscoveryStarted(String serviceType) {
                synchronized(WirelessHost.this) {
                    if(closed) try { nsd.stopServiceDiscovery(this); } catch(IllegalArgumentException ignored) {}
                }
            }
            public void onDiscoveryStopped(String serviceType) {}
            public void onStartDiscoveryFailed(String serviceType,int error) {
                listener.status("control","无线控制设备发现失败（"+error+"），请重新连接");
            }
            public void onStopDiscoveryFailed(String serviceType,int error) {}
            public void onServiceFound(NsdServiceInfo info) {
                synchronized(WirelessHost.this) {
                    if(closed || pending.size()>=32) return;
                    if(info.getServiceType()==null) info.setServiceType(type);
                    String serviceKey=key(info);
                    if(revisions.size()>=64 && !revisions.containsKey(serviceKey)) return;
                    revisions.put(serviceKey,++nextRevision);
                    pending.removeIf(item -> key(item).equals(serviceKey));
                    pending.add(info); resolveNext();
                }
            }
            public void onServiceLost(NsdServiceInfo info) {
                synchronized(WirelessHost.this) {
                    if(closed) return;
                    if(info.getServiceType()==null) info.setServiceType(type);
                    String serviceKey=key(info);
                    revisions.remove(serviceKey);
                    NsdServiceInfo old=resolved.remove(serviceKey);
                    pending.removeIf(item -> key(item).equals(serviceKey));
                    if(old!=null) deliver(old,true);
                }
            }
        };
        discoveries.add(discovery);
        nsd.discoverServices(type,NsdManager.PROTOCOL_DNS_SD,discovery);
    }

    @SuppressWarnings("deprecation")
    private synchronized void resolveNext() {
        if(closed || resolving || pending.isEmpty()) return;
        resolving=true;
        NsdServiceInfo next=pending.remove();
        String serviceKey=key(next);
        Long revision=revisions.get(serviceKey);
        try { nsd.resolveService(next,new NsdManager.ResolveListener() {
            public void onResolveFailed(NsdServiceInfo service,int error) { finished(null); }
            public void onServiceResolved(NsdServiceInfo service) { finished(service); }
            private void finished(NsdServiceInfo service) {
                synchronized(WirelessHost.this) {
                    resolving=false;
                    if(closed) return;
                    if(service!=null && service.getHost()!=null && Objects.equals(revision,revisions.get(serviceKey))) {
                        if(service.getServiceType()==null) service.setServiceType(next.getServiceType());
                        if(service.getServiceName()==null) service.setServiceName(next.getServiceName());
                        resolved.put(serviceKey,service);
                        deliver(service,false);
                    }
                    resolveNext();
                }
            }
        }); } catch(RuntimeException error) {
            resolving=false;
            listener.status("diagnostic","Wireless resolve: "+error);
            resolveNext();
        }
    }

    private void deliver(NsdServiceInfo info,boolean lost) {
        try {
            JSONObject json=new JSONObject();
            json.put("name",info.getServiceName()); json.put("type",info.getServiceType());
            json.put("host",info.getHost().getHostAddress()); json.put("port",info.getPort()); json.put("lost",lost);
            byte[] id=info.getAttributes().get("identifier");
            if(id!=null) json.put("identifier",new String(id,StandardCharsets.UTF_8));
            callbacks.execute(() -> { synchronized(WirelessHost.this) { if(closed) return; } listener.device(json.toString()); });
        } catch(Exception error) { listener.status("diagnostic","Wireless discovery: "+error); }
    }

    @Override public synchronized void close() {
        if(closed) return;
        closed=true;
        for(NsdManager.DiscoveryListener item:discoveries)
            try { nsd.stopServiceDiscovery(item); } catch(IllegalArgumentException ignored) {}
        for(NsdManager.RegistrationListener item:registrations)
            try { nsd.unregisterService(item); } catch(IllegalArgumentException ignored) {}
        pending.clear(); resolved.clear(); revisions.clear(); callbacks.shutdownNow();
        if(multicast.isHeld()) multicast.release();
    }
}
