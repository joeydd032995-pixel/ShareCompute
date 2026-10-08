# One model, three native compute workers

**Simplified setup:** use the standalone laptop test kit and QR-paired phone apps. See [Test kit quickstart](TEST-KIT-QUICKSTART.md). The build instructions below are for development.

Target hardware: a **4 GB laptop**, **8 GB Android phone**, and **8 GB iPhone**.
The iPhone executes assigned model layers using native C++ inside a Swift app.
An owned Mac is not required: GitHub Actions builds the app on a hosted macOS runner.

This is a bounded proof, not a production inference service. Start with the pinned
Qwen2.5-0.5B-Instruct Q4_K_M model (491,400,032 bytes), a 512-token context, and 24
generated tokens. The small model makes installation, transport, and numerical
verification possible before increasing memory use. It does not demonstrate a model
larger than any single device's capacity. The capacity test below does, and has so far run on
loopback only.

## What combining RAM means here

The three operating systems do not expose a shared 20 GB address space. Instead,
llama.cpp assigns different layers of one model to workers. Each worker stores its
assigned weights and runs its part of the inference graph; the runtime transfers
intermediate tensors between them. OS memory, runtime overhead, shared tensors, and
communication buffers reduce usable capacity. Wi-Fi adds latency and may make this
slower than local inference. Capacity and speed are separate measurements.

| Device | Role | Default RPC buffer budget |
|---|---|---:|
| Laptop | Coordinator, GGUF file, tokenizer, shared tensors, native layer worker | 768 MiB |
| Android | Native CPU layer worker under Termux | 2,048 MiB |
| iPhone | Native CPU layer worker in the foreground | 1,536 MiB |

These are limits on allocated **RPC buffers**, not whole-process RSS limits or promises
of available memory. The OS can still terminate an app. They include tracked weight,
KV, and compute buffers; code, graph metadata, TLS, and the laptop's non-RPC allocations
are additional. Model sizes and layer counts do not produce perfectly balanced bytes.

All binaries use llama.cpp revision
`4da6337767f973e2b4d0797e5b323d77d8565e4a` with two repository patches, applied in order by
`scripts/build_split_runtime.py`: `native/split/llama-budget.patch` (buffer budgets and counters)
and `native/split/llama-cache.patch` (the weight cache below).
CPU-only execution avoids dependence on incompatible device GPU runtimes.

## Weight cache

Each phone keeps the weights it receives in its app cache directory (Android `cacheDir/rpc-weights`,
iOS `Library/Caches/rpc-weights`). On the next run with the same model, the laptop asks before
sending each weight tensor over 1 MiB, and the phone loads it from storage instead. The phone checks
every cached file against its hash before using it; a damaged or partial file is deleted and sent
again, never computed with (F38). On loopback a warm run sent the Android worker 16 MB instead of
125 MB, and the iPhone worker 9 MB instead of 215 MB.

On the operator's physical Android phone and iPhone the second of two back-to-back runs sent them
24.4 MiB instead of 325.0 MiB (−92.5%) and took 18.4 s instead of 69.6 s. Every cached byte was a
hit and none was rejected (F39, `evidence/physical-weight-cache/`). The Wi-Fi saving there is a measured
byte count. Survival across an app kill or reboot, and the damaged-file case on a phone, have not
been observed.

Desktop workers started by `split_cluster.py` use a cache only with `--cache-dir DIR`, which gives
each worker `DIR/<node>`. The laptop's own worker leaves it off by default: its shard travels over
loopback, so caching it saves nothing. `report.json` records each worker's cache hits, stores and
rejections for the run under `workers.<node>.cache`.

## Capacity test

The 0.5B model fits on every worker, so it proves splitting and not pooling. The capacity profile
is Qwen2.5-3B-Instruct at **Q8_0** (3,616,088,480 bytes, pinned in `scripts/download_split_model.py`).
The Q4_K_M file of the same model was the first choice and is not enough: one worker holds it in
1,890 MiB, inside the Android budget (F40).

| One worker needs for the whole Q8_0 model | 3,183 MiB |
|---|---:|
| Largest single budget (Android) | 2,048 MiB |
| The three budgets together | 4,352 MiB |

On Linux loopback the three budgets pooled and ran it: peak allocations 562 / 1,348 / 1,299 MiB
(laptop / Android / iPhone), tokens equal to the reference (`evidence/capacity-loopback/`). The
physical run is the next step.

- **Reference, not baseline.** The laptop never runs this model unsplit. The coordinator takes
  `--reference native/split/reference-capacity.json`, tokens recorded from the same runtime on a Linux
  PC, and `report.json` says `"baseline_source": "pinned-reference"`.
- **What the claim is.** `report.json` has a `capacity` block. When the workers' combined peak
  allocations exceed the largest budget it says so and replaces the "small-model" note. This is a
  statement about worker budgets, not about physical memory limits.
- **Limits.** The generation is allowed an hour, since the first upload sends about 2,580 MiB to the
  phones. The laptop's real memory use is about 570 MiB for its worker and 391 MiB for the probe.
- **Reproduce on loopback:** `python3 scripts/download_split_model.py --profile capacity`, then
  `python3 scripts/verify_capacity.py --bin-dir build/desktop/bin --model models/split-capacity.gguf --out split-runs/capacity`.

## Get the builds

1. Check out `main`.
2. Open the [current Actions run](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/37707858657)
   for commit `7ba06cc` and download the matching
   [Windows](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/37707858657/artifacts/11521311221),
   [Linux](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/37707858657/artifacts/11521241439),
   [Android ARM64](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/37707858657/artifacts/11520542858), and
   [iPhone IPA](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/37707858657/artifacts/11520304646)
   artifacts as appropriate. Builds older than September 29 lack QR pairing, and
   builds older than October 4 lack the readable join errors and the Android
   foreground service. These CI artifacts expire; use a later successful run of
   the same workflow after they expire.
3. Desktop artifacts contain `sc-rpc-worker` and `sc-split-probe` (`.exe` on Windows).
   Extract these into `build/desktop/bin/` in your checkout. On Linux run
   `chmod +x build/desktop/bin/sc-*` after extracting.
4. The Android ARM64 artifact contains the same native executables. Android 9+ and
   a 64-bit ARM device are required by this build. Only the worker is needed there.
5. The iOS artifact contains `ShareCompute-unsigned.ipa`. **It must be signed before
   a physical iPhone can install it.** The simulator artifact is evidence, not an
   installable phone app. The app requires iOS 16+.

The Windows and Linux artifacts target x86-64; the CPU build uses a conservative
instruction set, including SSE4.2. A Chromebook requires a working Linux environment.
If your laptop is ARM-based, build natively for that platform instead of using x86-64
artifacts. Windows compilation requires Visual Studio C++ Build Tools, but running
the artifact does not require installing the compiler.

### Install the iPhone app without owning a Mac

Use your Windows or Linux laptop to bootstrap SideStore following its maintained
[prerequisites](https://docs.sidestore.io/docs/installation/prerequisites) and
[installation instructions](https://docs.sidestore.io/docs/installation/install).
They cover iloader, the USB connection, Apple Account sign-in, device trust,
Developer Mode, and the LocalDevVPN setup.

Save `ShareCompute-unsigned.ipa` in the iPhone's Files app, then use SideStore's
local IPA import in **My Apps** to sign and install it with your own account.
Keep signing credentials in the signing tool; this repository and its CI do not need
your Apple password, certificate, or provisioning profile. Free-account apps need
periodic refresh; SideStore's [FAQ](https://docs.sidestore.io/docs/faq) documents
the app limits. This CPU worker needs no JIT activation or special increased-memory
entitlement. The signing/install procedure still needs verification on your phone.

GitHub's [standard hosted runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
include macOS and are free for public repositories. This build route uses a cloud
Mac only during compilation; inference uses your three devices.

## Prepare the laptop

Install Python 3.11+ and, for the coordinator's certificate generation, cryptography.
On Linux, use `python3` wherever your installation does not provide `python`:

```sh
python -m pip install cryptography==46.0.0
python scripts/download_split_model.py
python scripts/split_cluster.py init --host 192.168.1.20
```

Replace `192.168.1.20` with your laptop's LAN address. All devices must be on a network
that permits peer connections. Allow inbound TCP 9443 on the laptop's private-network
firewall. No router port forwarding is needed. Keep the laptop awake.

`init` creates `.sc-pairing/cluster.json`, a TLS key/certificate, and separate
`laptop.json`, `android.json`, `iphone.json` pairing files. Transfer only `android.json`
to Android and only `iphone.json` to the iPhone's Files app, using a trusted channel.
These files contain credentials. Do not commit, share publicly, or include them in logs.
Each worker pins the laptop's certificate **before sending its credential**. The laptop
requires the per-worker token on both control and data connections. Pairing certificates
are created for 30 days; regenerate the directory and re-pair when rotating credentials.

To use smaller budgets, initialize a new directory with
`--budgets 512 1024 768` (laptop, Android, iPhone). Do not equate installed RAM with
available worker RAM. Changing an iPhone's budget requires restarting its app.

## Prepare Android

Use [Termux](https://termux.dev/) from its official distribution instructions.
Keep its session active and disable battery optimization for that session if Android
stops it. No full-model download is needed on either phone.

```sh
pkg install python git
git clone https://github.com/joeydd032995-pixel/ShareCompute.git
cd ShareCompute
mkdir -p build/android/bin
```

Copy the Android artifact's `sc-rpc-worker` to `build/android/bin/` under Termux's
private home, and copy its pairing file to `android.json` there. Do not execute the
binary from shared Downloads storage. Then:

```sh
chmod +x build/android/bin/sc-rpc-worker
python scripts/split_cluster.py worker --pair android.json --binary build/android/bin/sc-rpc-worker
```

Start the laptop coordinator first (below). If you prefer compiling on the phone,
install `clang cmake ninja` with `pkg`, then run
`python scripts/build_split_runtime.py --platform android --jobs 2`.
The runtime version is checked before joining. The Android worker uses Python's
standard TLS library and does not need the cryptography package.

## Run the physical proof

On the laptop, start the coordinator. On Windows PowerShell use this single line:

```powershell
python scripts/split_cluster.py coordinator --worker-binary build/desktop/bin/sc-rpc-worker.exe --probe-binary build/desktop/bin/sc-split-probe.exe --model models/split-proof.gguf --tokens 16 --out split-runs/physical-1
```

On Linux use:

```sh
python3 scripts/split_cluster.py coordinator --worker-binary build/desktop/bin/sc-rpc-worker --probe-binary build/desktop/bin/sc-split-probe --model models/split-proof.gguf --tokens 16 --out split-runs/physical-1
```

Start Android's worker command. Alternatively, install the Android APK and paste the contents of
`android.json` under its pairing-code field. On the iPhone open **ShareCompute Worker**, expand
**Paste code or import file**, import `iphone.json`, tap **Connect**, and allow local-network access. Keep the app in the
foreground. All three must join within 180 seconds; use `--join-timeout 600` if setup
needs longer. Use a new `--out` directory for each run.

The laptop runs a local baseline and then the split generation. The final
`report.json` passes only when it has all of:

- Nonempty, disjoint native layer assignments on the three RPC devices.
- Positive native graph-execution counts and tracked buffer allocations on each worker.
- Bidirectional traffic through every authenticated worker tunnel.
- A successful bounded generation with exactly the same greedy token IDs as the baseline.
- For physical mode, an Android worker and a nonsimulator iOS worker at distinct,
  nonloopback addresses. These platform flags are reported by trusted workers, not
  cryptographic hardware attestation.

CPU floating-point differences across architectures can change greedy choices in a
near tie. The Linux x86-64 and macOS ARM baselines matched for the first 19 tokens of
this fixture and differed at token 20. The physical commands use a 16-token fixture
for that reason. A mixed-CPU run can still differ sooner; the strict gate will fail
rather than quietly relax numerical validation. A 16-token PASS proves the bounded
three-worker execution, not numerical identity for every possible prompt or length.
Preserve `report.json` and `*.log` for diagnosis; do not publish the pairing directory.
Model-generated fixture text is not a factual answer or a model quality evaluation.

Backgrounding the iPhone disconnects it. A lost worker, allocation refusal, bad token,
wrong runtime, stalled heartbeat, timeout, or failed native decode fails the run.
No partial generation is accepted. Restart the coordinator and reconnect the workers
for another run; this proof does not claim live failover or in-process recovery.

## Build and verify from source

```sh
python scripts/build_split_runtime.py --jobs 2
python scripts/test_split_cluster.py -v
python scripts/verify_split_runtime.py --bin-dir build/desktop/bin --model models/split-proof.gguf --out split-runs/check
```

The matrix uses three native processes on one host: successful generation, worker
death **after observed computation**, and allocation refusal at 64 MiB per worker.
It deliberately labels these as loopback results. CI additionally builds Android,
Windows, and iOS and runs the actual Swift/native app in the iOS Simulator as the third
worker. All five jobs passed on the validated run. In the simulator report, all three
workers held distinct layers, each completed 24 native graphs, and the generated
tokens matched the local ARM baseline. Simulator success still does not prove
physical iPhone memory or LAN behavior.

The native RPC listener binds only to loopback on each device. TLS reverse tunnels
carry its bytes across the LAN; raw RPC is never opened to the network. This protects
against unpaired network clients. Upstream RPC remains experimental and assumes a
trusted client: the paired laptop can supply native graph instructions and must be
trusted. This is not a hostile multi-tenant compute sandbox.

Local execution evidence is under [evidence/three-device-split](evidence/three-device-split/).
A physical PASS from an actual laptop, Android phone and iPhone is recorded in
[evidence/physical-three-device](evidence/physical-three-device/) (F37).
