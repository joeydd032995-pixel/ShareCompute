# ShareCompute test kit

This kit is for a 4 GB laptop, 8 GB Android phone, and 8 GB iPhone. It splits one small model across all three native workers. No owned Mac, Python, Git, compiler, or Termux installation is needed for normal use.

## Downloads

[Current build](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/38026778633) from `main` at commit `c7fb789`. Sign in to GitHub to download artifacts. Take the laptop kit and both phone apps from this same build.

| Device | Download |
| --- | --- |
| Windows 10/11 x86-64 laptop | [Standalone Windows kit](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/38026778633/artifacts/11660254154) |
| Ubuntu 24.04+ x86-64 laptop | [Standalone Linux kit](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/38026778633/artifacts/11661396530) |
| Android 9+ ARM64 | [Worker APK](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/38026778633/artifacts/11660756347) |
| iPhone iOS 16+ | [Worker IPA (requires signing)](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/38026778633/artifacts/11660432499) |

Both packaged laptop executables passed real-model three-process inference tests in CI, and the iPhone app passed native inference in the iOS simulator. On 2026-10-07 the kit passed on a real Windows laptop, Android phone and iPhone ([evidence](evidence/physical-three-device/)), and on 2026-10-08 a build with the weight cache passed twice in a row on the same devices ([evidence](evidence/physical-weight-cache/)). The reports do not record the exact commit of the build the operator used. These artifacts expire on 2027-01-08; a later successful run of the same workflow on `main` produces replacements.

## First use

1. Download the **ShareCompute-Windows** or **ShareCompute-Linux** kit from the linked successful build. Extract the artifact ZIP, then the kit ZIP inside it. Keep the entire `ShareCompute` folder together. Windows 10/11 x86-64: double-click `Start.cmd`. Linux x86-64 (Ubuntu 24.04 or newer): run `sh Start.sh` (if permissions were lost, first run `chmod +x ShareCompute`). Your browser opens the local dashboard.
2. Use **phone downloads** in the dashboard, or the table above, to get the matching **Android APK** and **iPhone IPA**. On the iPhone, delete any older ShareCompute Worker first: the current app shows **Scan laptop QR** at the top, while an old build shows **Import iphone.json** instead. Android 9+ ARM64: extract the ZIP, install the APK, and permit installation from that download source if prompted. iPhone (iOS 16+): install the IPA through [SideStore](https://docs.sidestore.io/docs/installation/prerequisites). Its initial setup needs your laptop, Apple account, a USB cable, and the documented signing/device-trust/Developer Mode steps. Windows and Linux are supported; you do not need your own Mac. Free-account signing requires periodic refresh. This Apple step cannot be removed by the launcher. The IPA alone is not directly installable.
3. On the iPhone, turn on Settings › Privacy & Security › Local Network › ShareCompute Worker. Put all three devices on the same Wi-Fi with no VPN on the phones. Click **Start test** on the laptop. The verified 469 MiB model downloads once. In each phone app, tap **Scan laptop QR** and scan that phone's code on the laptop screen. Allow camera, local-network and (on Android) notification access when asked. Keep the iPhone app open on screen until the result appears; iOS disconnects it in the background. The Android app keeps computing if you lock that phone or switch apps, and shows a "ShareCompute is computing" notification with a **Disconnect** button. Click **Download report**.

If Windows asks about network access, permit the launcher on your private network. On Linux, an active firewall must allow incoming TCP 9443 from your local network. Guest Wi-Fi/client isolation can block the phones; use a network where devices can reach each other. The launcher detects the laptop's Wi-Fi address, prints it in its window, and pre-fills it in the dashboard, with any other addresses listed below the box. If a VPN or virtual adapter is chosen instead, pick your Wi-Fi address from that list.

## Every later test

Open the laptop launcher, click **Start test**, then scan/connect both phone apps. The model is reused. Each phone keeps the model data it was sent, so a repeat test with the same model uploads only a small fraction over Wi-Fi and starts much sooner; the first test after installing, clearing, or changing the model still uploads everything. On the real devices, the second run sent the phones 24.4 MiB instead of 325.0 MiB and finished in 18 s instead of 70 s ([evidence](evidence/physical-weight-cache/)). Both apps show how much is cached and have a **Clear cached model data** button. If the iPhone signature expired, refresh it in SideStore first. No development environment is required. Tap Connect again after a test disconnects.

## The 3B capacity test

The quick test uses a model that fits on any one device. The capacity test uses Qwen2.5-3B at Q8_0, which no single worker budget can hold (3,183 MiB needed; the largest budget is 2,048 MiB), so a PASS means the three devices computed something none of them could have run alone within these limits. It has passed twice on the operator's own laptop, Android phone and iPhone ([evidence](evidence/physical-3b-capacity/); the loopback rehearsal is [here](evidence/capacity-loopback/)). Allow about 8 minutes for the first run and 2–3 for later ones, once the phones have the weights cached.

1. Before you start: **close other programs on the laptop**. Its worker and the launcher need about 1.1 GB of real memory, and the laptop had roughly 1.2 GB free in the last test. The laptop also needs 3.7 GB of free disk space for the model.
2. In the dashboard choose **3B capacity test (3.4 GB)** next to the Wi-Fi address, then **Start test**. The model downloads once (several minutes), then you scan the QR codes as usual.
3. **The first run is slow: about 2,580 MiB (2.5 GiB) goes to the phones over Wi-Fi.** Expect 10 minutes or more. Keep the iPhone app on screen and leave the phones plugged in and on the same Wi-Fi. The generation limit is one hour. A second run reuses what the phones kept and is much faster.
4. The result: **PASS** shows `"capacity": {"exceeds_largest_worker_budget": true}` in `report.json`. There is no `baseline.log`, because the laptop never runs the model alone; the 16 tokens are compared with ones recorded on a Linux PC.
5. If it fails, **Download report** and keep the logs. A failure that says `Split greedy tokens differ from the pinned reference` is either a real fault or a harmless numerical difference between processors; the logs from the run are needed to tell which.

The phones will keep about 2.5 GB of cached model data afterwards. Use **Clear cached model data** in each app when you are finished.

## What PASS means

Each physical phone and the laptop completed native model computation with nonzero assigned layers, graph counters, memory allocations, and tunnel traffic; the 16 generated tokens matched a laptop-only baseline. The report records the proof. Phone identities are worker reports, not cryptographic hardware attestation.

This describes the quick test, which is a small-model splitting proof, not a claim that the devices expose one pooled RAM address space, can run any large model, or will be faster than the laptop alone. Native buffer budgets are 768 MiB / 2048 MiB / 1536 MiB; these are not whole-app RAM limits. Exact token matching on mixed CPU architectures can still fail, and a failure must be investigated rather than treated as a pass.

## Troubleshooting and removal

- If a phone fails, read its screen: the apps name the cause, for example "Could not reach the laptop at …", "The laptop refused this iPhone" or "The laptop certificate does not match the scanned code". The laptop's report logs list every refused connection with its reason.
- Waiting for phones: check the Wi-Fi address, firewall, that the iPhone app is on screen, and that each app scanned its own QR. The join window is 15 minutes. Close the launcher with **Close launcher**, not merely by closing its browser tab.
- Camera unavailable: expand the dashboard's pairing-code field, copy it to the matching app, and tap Connect. Treat the QR/code as private credentials.
- The model is downloaded only on the laptop. Internet is needed for the first download and phone installation, not for the subsequent local test.
- Some Android brands stop background apps aggressively despite the notification. If the Android worker drops while locked, set its battery usage to unrestricted.
- These are test builds. A newly built Android APK may use a different test signing key; uninstall the previous test app first if Android refuses an update. Re-scan after reinstalling.
- Cached model data is checked before every use, so a damaged file is simply sent again. If a phone runs short of storage, use **Clear cached model data**; the OS may also clear it on its own, which only makes the next test slower.
- Download report includes logs and the result, not pairing tokens or the laptop certificate's private key. Logs contain local paths and device IP addresses; review before sharing.
- Laptop data is isolated in `%LOCALAPPDATA%\ShareCompute` on Windows or `~/.local/share/ShareCompute` on Linux. To remove it, close the launcher and delete that folder plus the extracted kit. Uninstall the phone apps normally.
- Advanced build/manual setup: [THREE-DEVICE-MODEL-SPLIT.md](THREE-DEVICE-MODEL-SPLIT.md).
