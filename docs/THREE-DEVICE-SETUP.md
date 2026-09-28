# ShareCompute on one 4 GB laptop, one 8 GB Android, and one 8 GB iPhone

This is the **usable, no-Mac path**. The laptop runs a small web gateway, the
Android can run a separate local model, and the iPhone opens the gateway in Safari.
The laptop can also run its own small model. You choose which model answers each
request. There is **no additive 20 GB RAM pool**, distributed model execution,
on-device iPhone compute, or background iPhone worker in this mode. The existing
MLX/Xcode app and the portable simulation do not provide that on these devices.

## Prepare a small model

Start with [Qwen2.5 0.5B Instruct Q4_K_M](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/tree/main).
Download `qwen2.5-0.5b-instruct-q4_k_m.gguf` to a folder on the laptop and, if
using Android compute, separately to Termux's home directory. It is a small
smoke-test model, not a strong general assistant. Larger quantized models may
fit Android's actual available memory, but the 8 GB hardware specification is
not the amount an app can safely allocate. Keep context at 1024 for the first run.

Install [Python 3](https://www.python.org/downloads/) on the laptop. Install a
current [llama.cpp release](https://github.com/ggml-org/llama.cpp/releases) for
your laptop OS; on Windows x64, use the CPU x64 binary archive and locate
`llama-server.exe`. No Swift, Xcode, Mac, cloud account, or paid service is needed.

## Laptop inference (optional but useful as a fallback)

Windows PowerShell, from the extracted llama.cpp binary folder:

```powershell
.\llama-server.exe -m C:\path\to\qwen2.5-0.5b-instruct-q4_k_m.gguf -c 1024 -b 128 -ub 64 -t 2 --host 127.0.0.1 --port 8080
```

Linux, from the directory containing the llama.cpp binaries:

```bash
./llama-server -m /path/to/qwen2.5-0.5b-instruct-q4_k_m.gguf -c 1024 -b 128 -ub 64 -t 2 --host 127.0.0.1 --port 8080
```

Wait for `http://127.0.0.1:8080/health` to report `{"status":"ok"}`.
For a 4 GB laptop, close memory-heavy apps and use one model at a time. If even
this fails to load, run inference only on Android and keep the gateway on the
laptop. Model files stay on their respective compute devices.

## Android inference (optional)

Install Termux, then build llama.cpp inside Termux (CPU mode):

```bash
pkg update
pkg install git cmake clang make ninja libandroid-spawn curl
git clone --depth=1 https://github.com/ggml-org/llama.cpp.git
cd llama.cpp
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_RPC=OFF -DLLAMA_CURL=OFF
cmake --build build --target llama-server -j2
curl -L https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/qwen2.5-0.5b-instruct-q4_k_m.gguf -o ~/qwen2.5-0.5b-instruct-q4_k_m.gguf
```

Find the Android phone's Wi-Fi address in Android Settings, then run (replace
`ANDROID_IP` and choose your own long secret for `ANDROID_KEY`):

```bash
./build/bin/llama-server -m ~/qwen2.5-0.5b-instruct-q4_k_m.gguf -c 1024 -b 128 -ub 64 -t 2 --host ANDROID_IP --port 8081 --api-key ANDROID_KEY
```

Keep Termux foregrounded and keep the phone awake and cool for this first
test. Android may suspend a background terminal or drop Wi-Fi, so the gateway
reports that backend as unavailable rather than assuming it remains alive.
Some Android builds may need different compiler settings; this path is not yet
verified on your specific phone.

## Start the gateway on the laptop

Find the laptop's Wi-Fi address (`ipconfig` on Windows, `ip addr` on Linux).
The phones and laptop must be on the same private Wi-Fi, without client
isolation. In a *new* terminal at the ShareCompute repository root:

Windows PowerShell:

```powershell
$env:SHARECOMPUTE_LAPTOP_URL = 'http://127.0.0.1:8080'
$env:SHARECOMPUTE_ANDROID_URL = 'http://ANDROID_IP:8081'
$env:SHARECOMPUTE_ANDROID_KEY = 'ANDROID_KEY'
py -3 scripts/device_gateway.py --host LAPTOP_IP --port 8765
```

Linux:

```bash
export SHARECOMPUTE_LAPTOP_URL=http://127.0.0.1:8080
export SHARECOMPUTE_ANDROID_URL=http://ANDROID_IP:8081
export SHARECOMPUTE_ANDROID_KEY=ANDROID_KEY
python3 scripts/device_gateway.py --host LAPTOP_IP --port 8765
```

Omit the laptop URL if only Android has a model, or omit the Android URL and
key if only the laptop does. The gateway prints a fresh access code at startup;
enter it in the page on **both** phones at `http://LAPTOP_IP:8765/`. Tap
**Check devices**, choose a ready device, and send a message. The page stores
the conversation only in the current browser tab's memory. The laptop relays
requests and receives output even when Android does the inference.

The laptop firewall may ask for permission for port 8765. Allow it only on
the private network. Android's port 8081 must be reachable from the laptop.
Stop both servers when finished. Do not forward either port to the internet.
This first setup uses HTTP on the local Wi-Fi: the access code, Android key,
prompts, and replies are visible to someone who can observe that network.
Use a Wi-Fi network you control; TLS or an authenticated private tunnel is
required before using this outside a trusted local network.

The `ggml-rpc-server` prototype is **not** part of these instructions. It is
still experimental, fragile, and unauthenticated. A future RAM-pooling
experiment must isolate that transport, measure actual Wi-Fi throughput and
failure behavior, and show it is worthwhile on these exact devices. An iPhone
Safari page cannot act as a reliable background native compute worker.

## Verify without physical devices

`python3 scripts/test_device_gateway.py` runs a fake llama-server and checks
the gateway's authentication, backend discovery, routing, and bad input. It
does not establish Android or iPhone performance. After device setup, verify
both phones can open the page, that Android responds through the gateway, and
that disconnecting Android marks it unavailable on the next check.
