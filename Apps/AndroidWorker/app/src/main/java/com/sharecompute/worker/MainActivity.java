package com.sharecompute.worker;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.hardware.Camera;
import android.os.Bundle;
import android.view.*;
import android.widget.*;
import com.google.zxing.*;
import com.google.zxing.common.HybridBinarizer;

@SuppressWarnings("deprecation")
public final class MainActivity extends Activity implements SurfaceHolder.Callback {
    private EditText code;
    private TextView status;
    private Camera camera;
    private SurfaceView preview;
    private boolean scanning;
    @Override public void onCreate(Bundle state) { super.onCreate(state); showForm(""); }
    private void showForm(String text) {
        scanning=false;
        LinearLayout layout=new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL); layout.setPadding(28,48,28,28);
        ScrollView scroll=new ScrollView(this); scroll.addView(layout); setContentView(scroll);
        TextView title=new TextView(this); title.setText("ShareCompute Worker"); title.setTextSize(28); layout.addView(title);
        TextView help=new TextView(this); help.setText("Start the test on your laptop, then scan its Android QR. While connected the test keeps running if you lock this phone or switch apps."); layout.addView(help);
        Button scan=new Button(this); scan.setText("Scan laptop QR"); layout.addView(scan); scan.setOnClickListener(v->{
            WorkerService.stop(this);
            if(checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED) requestPermissions(new String[]{Manifest.permission.CAMERA},1);
            else showScanner();
        });
        code=new EditText(this); code.setHint("Or paste pairing code"); code.setText(text); code.setMaxLines(5); code.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE|android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); layout.addView(code);
        Button connect=new Button(this); connect.setText("Connect"); layout.addView(connect); connect.setOnClickListener(v->connect());
        Button stop=new Button(this); stop.setText("Disconnect"); layout.addView(stop); stop.setOnClickListener(v->WorkerService.stop(this));
        TextView cached=new TextView(this); cached.setText(cacheText()); layout.addView(cached);
        Button clear=new Button(this); clear.setText("Clear cached model data"); layout.addView(clear);
        clear.setOnClickListener(v->{ long freed=WeightCache.clear(this); cached.setText(cacheText()); status.setText("Cleared "+(freed/1048576)+" MiB. The next test uploads the model again."); });
        status=new TextView(this); status.setTextSize(18); layout.addView(status); WorkerService.observe(this::showStatus);
        getWindow().getDecorView().setOnApplyWindowInsetsListener((v,insets)->{layout.setPadding(28,28+insets.getSystemWindowInsetTop(),28,28+insets.getSystemWindowInsetBottom());return insets;});
    }
    private void connect() {
        String text=code.getText().toString();
        try { WorkerSession.parse(text); } catch(Exception e) { status.setText(e.getMessage()); return; }
        // Without this permission the service still runs; only its notification is hidden.
        if(android.os.Build.VERSION.SDK_INT>=33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)!=PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS},2);
        try { WorkerService.start(this,text); } catch(Exception e) { status.setText("Could not start the worker: "+e.getMessage()); }
    }
    private void showStatus(String message) { if(status!=null) status.setText(message); }
    private String cacheText() { return "Cached model data: "+(WeightCache.bytes(this)/1048576)+" MiB. Repeat tests with the same model reuse it."; }
    @Override protected void onStart() {super.onStart();WorkerService.observe(this::showStatus);}
    // The worker service keeps computing in the background; only the camera is released here.
    @Override protected void onStop() {super.onStop();WorkerService.observe(null);releaseCamera();}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants) {
        super.onRequestPermissionsResult(request,permissions,grants);
        if(request!=1) return;
        if(grants.length>0 && grants[0]==PackageManager.PERMISSION_GRANTED) showScanner(); else status.setText("Camera denied. Paste the pairing code instead.");
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
                    String text=result.getText();WorkerSession.parse(text);scanning=false;releaseCamera();showForm(text);connect();
                } catch(ReaderException ignored) {} catch(Exception e) {scanning=false;releaseCamera();showForm("");status.setText("Invalid Android QR: "+e.getMessage());}
            });camera.startPreview();
        } catch(Exception e) {releaseCamera();showForm("");status.setText("Camera unavailable. Paste pairing code instead.");}
    }
    @Override public void surfaceChanged(SurfaceHolder h,int format,int width,int height) {}
    @Override public void surfaceDestroyed(SurfaceHolder h) {releaseCamera();}
    private void releaseCamera() {if(camera!=null){camera.setPreviewCallback(null);camera.stopPreview();camera.release();camera=null;}}
}
