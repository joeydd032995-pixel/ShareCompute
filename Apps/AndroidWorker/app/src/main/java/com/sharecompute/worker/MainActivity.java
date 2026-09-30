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
import org.json.JSONObject;
import java.net.ServerSocket;

@SuppressWarnings("deprecation")
public final class MainActivity extends Activity implements SurfaceHolder.Callback {
    private EditText code;
    private TextView status;
    private WorkerSession session;
    private Camera camera;
    private SurfaceView preview;
    private boolean scanning;
    private int nativePort;
    private long nativeBudget;
    @Override public void onCreate(Bundle state) { super.onCreate(state); showForm(""); }
    private void showForm(String text) {
        scanning=false;
        LinearLayout layout=new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL); layout.setPadding(28,48,28,28);
        ScrollView scroll=new ScrollView(this); scroll.addView(layout); setContentView(scroll);
        TextView title=new TextView(this); title.setText("ShareCompute Worker"); title.setTextSize(28); layout.addView(title);
        TextView help=new TextView(this); help.setText("Start the test on your laptop, then scan its Android QR. Keep this app open while computing."); layout.addView(help);
        Button scan=new Button(this); scan.setText("Scan laptop QR"); layout.addView(scan); scan.setOnClickListener(v->{
            disconnect();
            if(checkSelfPermission(Manifest.permission.CAMERA)!=PackageManager.PERMISSION_GRANTED) requestPermissions(new String[]{Manifest.permission.CAMERA},1);
            else showScanner();
        });
        code=new EditText(this); code.setHint("Or paste pairing code"); code.setText(text); code.setMaxLines(5); code.setInputType(android.text.InputType.TYPE_CLASS_TEXT|android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE|android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS); layout.addView(code);
        Button connect=new Button(this); connect.setText("Connect"); layout.addView(connect); connect.setOnClickListener(v->connect());
        Button stop=new Button(this); stop.setText("Disconnect"); layout.addView(stop); stop.setOnClickListener(v->{disconnect();status.setText("Disconnected");});
        status=new TextView(this); status.setText("Ready to pair"); status.setTextSize(18); layout.addView(status);
        getWindow().getDecorView().setOnApplyWindowInsetsListener((v,insets)->{layout.setPadding(28,28+insets.getSystemWindowInsetTop(),28,28+insets.getSystemWindowInsetBottom());return insets;});
    }
    private void connect() {
        disconnect();
        try {
            JSONObject p=WorkerSession.parse(code.getText().toString()); long budget=p.getLong("budget_mib")*1048576;
            if(nativePort!=0 && nativeBudget!=budget) throw new Exception("Restart the app to change its memory budget");
            if(nativePort==0) {
                try(ServerSocket listener=new ServerSocket(0,1,java.net.InetAddress.getByName("127.0.0.1"))) {nativePort=listener.getLocalPort();}
                nativeBudget=budget;
                new Thread(()->{int result=NativeWorker.run(nativePort,nativeBudget);runOnUiThread(()->{disconnect();status.setText("Native worker exited ("+result+"). Restart the app.");});},"native-worker").start();
            }
            status.setText("Connecting…"); getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
            final WorkerSession[] holder=new WorkerSession[1];
            holder[0]=new WorkerSession(p,nativePort,message->runOnUiThread(()->{if(session==holder[0])status.setText(message);}));
            session=holder[0]; session.start();
        } catch(Exception e) {status.setText(e.getMessage());}
    }
    private void disconnect() { if(session!=null)session.stop(); session=null; getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON); }
    @Override protected void onStop() {super.onStop();disconnect();releaseCamera();if(status!=null)status.setText("App left foreground. Tap Connect for the next test.");}
    @Override public void onRequestPermissionsResult(int request,String[] permissions,int[] grants) {super.onRequestPermissionsResult(request,permissions,grants);if(request==1 && grants.length>0 && grants[0]==PackageManager.PERMISSION_GRANTED)showScanner();else status.setText("Camera denied. Paste the pairing code instead.");}
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
