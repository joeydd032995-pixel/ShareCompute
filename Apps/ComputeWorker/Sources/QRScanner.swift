import SwiftUI
import AVFoundation

struct QRScanner: UIViewControllerRepresentable {
    let receive: (String) -> Void
    func makeUIViewController(context: Context) -> ScannerController { ScannerController(receive: receive) }
    func updateUIViewController(_ controller: ScannerController, context: Context) {}
    static func dismantleUIViewController(_ controller: ScannerController, coordinator: ()) { controller.stop() }
}
final class ScannerController: UIViewController, AVCaptureMetadataOutputObjectsDelegate {
    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "ShareCompute.camera")
    private var preview: AVCaptureVideoPreviewLayer?
    private let receive: (String) -> Void
    private var finished = false
    private var stopped = false // accessed on camera queue
    init(receive: @escaping (String) -> Void) { self.receive = receive; super.init(nibName: nil, bundle: nil) }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func viewDidLoad() {
        super.viewDidLoad(); view.backgroundColor = .black
        AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
            guard let self else { return }
            if granted { self.queue.async { self.configure() } }
            else { DispatchQueue.main.async { self.message("Camera access denied. Close this screen and paste the pairing code instead.") } }
        }
    }
    private func configure() {
        guard !stopped, let device = AVCaptureDevice.default(for: .video),
              let input = try? AVCaptureDeviceInput(device: device), session.canAddInput(input) else {
            DispatchQueue.main.async { self.message("Camera unavailable. Paste the pairing code instead.") }; return
        }
        session.beginConfiguration(); session.addInput(input)
        let output = AVCaptureMetadataOutput()
        guard session.canAddOutput(output) else { session.commitConfiguration(); return }
        session.addOutput(output); output.setMetadataObjectsDelegate(self, queue: .main); output.metadataObjectTypes = [.qr]
        session.commitConfiguration()
        DispatchQueue.main.async {
            let layer = AVCaptureVideoPreviewLayer(session: self.session); layer.videoGravity = .resizeAspectFill
            layer.frame = self.view.bounds; self.view.layer.addSublayer(layer); self.preview = layer
        }
        session.startRunning()
    }
    override func viewDidLayoutSubviews() { super.viewDidLayoutSubviews(); preview?.frame = view.bounds }
    func stop() { queue.async { self.stopped = true; self.session.stopRunning() } }
    private func message(_ text: String) { let label = UILabel(frame: view.bounds.insetBy(dx: 24, dy: 24)); label.text = text; label.textColor = .white; label.numberOfLines = 0; view.addSubview(label) }
    func metadataOutput(_ output: AVCaptureMetadataOutput, didOutput objects: [AVMetadataObject], from connection: AVCaptureConnection) {
        guard !finished, let qr = objects.first as? AVMetadataMachineReadableCodeObject, let text = qr.stringValue else { return }
        finished = true; stop(); receive(text)
    }
}
