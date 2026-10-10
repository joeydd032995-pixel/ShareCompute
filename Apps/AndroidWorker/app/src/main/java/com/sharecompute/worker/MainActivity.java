package com.sharecompute.worker;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.hardware.Camera;
import android.net.Uri;
import android.os.Bundle;
import android.view.*;
import android.widget.*;
import com.google.zxing.*;
import com.google.zxing.common.HybridBinarizer;
import org.json.JSONObject;

@SuppressWarnings("deprecation")
public final class MainActivity extends Activity implements SurfaceHolder.Callback {
    private EditText code;
    private TextView status;
    private TextView cached;
    private Camera camera;
    private SurfaceView preview;
    private boolean scanning;
    @Override public void onCreate(Bundle state) { super.onCreate(state); EventLog.init(this); EventLog.event("activity_create"); showForm(""); }
    private void showForm(String text) {
        scanning=false;
        LinearLayout layout=new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL); layout.setPadding(28,48,28,28);
        ScrollView scroll=new ScrollView(this); scroll.addView(layout); setContentView(scroll);
        TextView title=new TextView(this); title.setText("ShareCompute Worker"); title.setTextSize(28); layout.addView(title);
        TextView help=new TextView(this); help.setText("Start the test on your laptop, then scan its Android QR. While connected the test keeps running if you lock this phone or switch apps."); layout.addView(help);
        Button scan=new Button(this); scan.setText("Scan laptop QR"); layout.addView(scan); scan.setOnClickListener(v->{
            EventLog.event("scan_tapped");
            WorkerService.stop(this);
            if(checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED) requestPermissions(new String[]{Manifest.permission.CAMERA},1);
            else showScanner();
        });
        code=new EditText(this); code.setHint("Or paste pairing code"); code.setText(text); code.setMaxLines(5); code.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE|android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); layout.addView(code);
        Button connect=new Button(this); connect.setText("Connect"); layout.addView(connect); connect.setOnClickListener(v->connect("paste"));
        Button stop=new Button(this); stop.setText("Disconnect"); layout.addView(stop); stop.setOnClickListener(v->{ EventLog.event("disconnect_tapped"); WorkerService.stop(this); });
        cached=new TextView(this); layout.addView(cached); refreshCache();
        Button clear=new Button(this); clear.setText("Clear cached model data"); layout.addView(clear);
        clear.setOnClickListener(v->{ EventLog.event("clear_cache_tapped"); long freed=WeightCache.clear(this); refreshCache(); status.setText("Cleared "+(freed/1048576)+" MiB. The next test uploads the model again."); });
        Button export=new Button(this); export.setText("Export log"); layout.addView(export); export.setOnClickListener(v->exportLog());
        TextView exportHelp=new TextView(this); exportHelp.setText("Opens the share sheet so you can send the log to the person helping you. The pairing code is left out of it."); layout.addView(exportHelp);
        status=new TextView(this); status.setTextSize(18); layout.addView(status); WorkerService.observe(this::showStatus);
        getWindow().getDecorView().setOnApplyWindowInsetsListener((v,insets)->{layout.setPadding(28,28+insets.getSystemWindowInsetTop(),28,28+insets.getSystemWindowInsetBottom());return insets;});
    }
    private void connect(String source) {
        String text=code.getText().toString();
        // parse() logs the reason when it refuses; this line records that a pairing code arrived and passed. The code itself is never logged.
        try { JSONObject p=WorkerSession.parse(text); EventLog.event("pairing_valid","source",source,"host",p.getString("host"),"port",p.getInt("port"),"budget_mib",p.getInt("budget_mib"),"chars",text.length()); }
        catch(Exception e) { status.setText(e.getMessage()); return; }
        // Without this permission the service still runs; only its notification is hidden.
        if(android.os.Build.VERSION.SDK_INT>=33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},2);
        try { WorkerService.start(this,text); } catch(Exception e) { EventLog.event("start_failed","cause",EventLog.cause(e)); status.setText("Could not start the worker: "+e.getMessage()); }
    }
    // Works whether or not the service is running: it only reads the log files, which are written either way.
    // Built off the main thread: EventLog.export waits for queued lines to reach the file and then copies
    // up to a megabyte, and doing that on the UI thread risks an ANR on a slow phone or a full disk.
    private void exportLog() {
        status.setText("Preparing the log…");
        new Thread(() -> {
            try {
                java.io.File file=EventLog.export(this);
                runOnUiThread(() -> offerLog(file));
            } catch(Exception e) {
                EventLog.event("export_failed","cause",EventLog.cause(e));
                runOnUiThread(() -> status.setText("Could not prepare the log: "+e.getMessage()));
            }
        },"log-export").start();
    }
    private void offerLog(java.io.File file) {
        try {
            Uri uri=LogProvider.uriFor(this,file);
            Intent send=new Intent(Intent.ACTION_SEND).setType("text/plain").putExtra(Intent.EXTRA_STREAM,uri).putExtra(Intent.EXTRA_SUBJECT,"ShareCompute Android log")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            send.setClipData(ClipData.newRawUri("log",uri)); // Lets the chooser pass the read grant on to the app chosen.
            startActivity(Intent.createChooser(send,"Send the log to…").addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION));
            status.setText("Log ready ("+(file.length()/1024)+" KiB). Choose where to send it.");
        } catch(Exception e) { EventLog.event("export_failed","cause",EventLog.cause(e)); status.setText("Could not prepare the log: "+e.getMessage()); }
    }
    // Status arrives as runs start, progress and end, so the cache total follows each run without its own timer.
    private void showStatus(String message) { if(status!=null) status.setText(message); refreshCache(); }
    private void refreshCache() { if(cached!=null) cached.setText("Cached model data: "+(WeightCache.bytes(this)/1048576)+" MiB. Repeat tests with the same model reuse it."); }
    @Override public void onTrimMemory(int level) {super.onTrimMemory(level);EventLog.event("trim_memory","who","activity","level",EventLog.trimLevel(level));}
    @Override protected void onStart() {super.onStart();EventLog.event("activity_start");WorkerService.observe(this::showStatus);refreshCache();}
    // The worker service keeps computing in the background; only the camera is released here.
    @Override protected void onStop() {super.onStop();EventLog.event("activity_stop");WorkerService.observe(null);releaseCamera();}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants) {
        super.onRequestPermissionsResult(request,permissions,grants);
        if(request!=1) return;
        if(grants.length>0 && grants[0]==PackageManager.PERMISSION_GRANTED) showScanner(); else { EventLog.event("camera_denied"); status.setText("Camera denied. Paste the pairing code instead."); }
    }
    private void showScanner() {
        scanning=true; LinearLayout layout=new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL);layout.setPadding(16,48,16,48);
        Button cancel=new Button(this);cancel.setText("Cancel scan");layout.addView(cancel);cancel.setOnClickListener(v->{releaseCamera();showForm("");});
        preview=new SurfaceView(this);layout.addView(preview,new LinearLayout.LayoutParams(-1,0,1));setContentView(layout);preview.getHolder().addCallback(this);
    }
    @Override public void surfaceCreated(SurfaceHolder holder) {
        if(!scanning)return;
        try {
            camera=Camera.open();Camera.Parameters p=camera.getParameters();
            if(p.getSupportedFocusModes().contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE))p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
            camera.setParameters(p);camera.setDisplayOrientation(90);camera.setPreviewDisplay(holder);
            camera.setPreviewCallback((bytes,c)->{
                if(!scanning)return;
                try {
                    Camera.Size size=c.getParameters().getPreviewSize();
                    PlanarYUVLuminanceSource source=new PlanarYUVLuminanceSource(bytes,size.width,size.height,0,0,size.width,size.height,false);
                    Result result=new com.google.zxing.qrcode.QRCodeReader().decode(new BinaryBitmap(new HybridBinarizer(source)));
                    String text=result.getText();WorkerSession.parse(text);scanning=false;releaseCamera();showForm(text);EventLog.event("scan_read");connect("scan");
                } catch(ReaderException ignored) {} catch(Exception e) {scanning=false;releaseCamera();showForm("");EventLog.event("scan_invalid");status.setText("Invalid Android QR: "+e.getMessage());}
            });camera.startPreview();
        } catch(Exception e) {releaseCamera();showForm("");EventLog.event("camera_unavailable","cause",EventLog.cause(e));status.setText("Camera unavailable. Paste pairing code instead.");}
    }
    @Override public void surfaceChanged(SurfaceHolder h,int format,int width,int height) {}
    @Override public void surfaceDestroyed(SurfaceHolder h) {releaseCamera();}
    private void releaseCamera() {if(camera!=null){camera.setPreviewCallback(null);camera.stopPreview();camera.release();camera=null;}}
}
