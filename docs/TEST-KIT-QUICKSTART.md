# ShareCompute test kit

This kit is for a 4 GB laptop, 8 GB Android phone, and 8 GB iPhone. It splits one small model across all three native workers. No owned Mac, Python, Git, compiler, or Termux installation is needed for normal use.

## Downloads

[Validated build](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/36526074904), runtime/app source commit `cca346aebf8621381ce9b80cbcd007a3432decec`. Sign in to GitHub to download artifacts. Use matching apps from this build.

| Device | Download |
| --- | --- |
| Windows 10/11 x86-64 laptop | [Standalone Windows kit](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/36526074904/artifacts/11014613302) |
| Ubuntu 24.04+ x86-64 laptop | [Standalone Linux kit](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/36526074904/artifacts/11014955984) |
| Android 9+ ARM64 | [Worker APK](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/36526074904/artifacts/11014498246) |
| iPhone iOS 16+ | [Worker IPA — requires signing](https://github.com/joeydd032995-pixel/ShareCompute/actions/runs/36526074904/artifacts/11014885851) |

Both packaged laptop executables passed real-model three-process inference tests; the iPhone simulator passed native inference using the QR payload decoder. The Android APK builds and includes its native ARM64 library. Your physical three-device test is the remaining hardware validation step. These test artifacts expire on December 28, 2026; a later workflow run can produce replacements.

## First use

1. Download the **ShareCompute-Windows** or **ShareCompute-Linux** kit from the linked successful build. Extract the artifact ZIP, then the kit ZIP inside it. Keep the entire `ShareCompute` folder together. Windows 10/11 x86-64: double-click `Start.cmd`. Linux x86-64 (Ubuntu 24.04 or newer): run `sh Start.sh` (if permissions were lost, first run `chmod +x ShareCompute`). Your browser opens the local dashboard.
2. Use **phone downloads** in the dashboard to get the matching **Android APK** and **iPhone IPA**. Android 9+ ARM64: extract the ZIP, install the APK, and permit installation from that download source if prompted. iPhone (iOS 16+): install the IPA through [SideStore](https://docs.sidestore.io/docs/installation/prerequisites). Its initial setup needs your laptop, Apple account, a USB cable, and the documented signing/device-trust/Developer Mode steps. Windows and Linux are supported; you do not need your own Mac. Free-account signing requires periodic refresh. This Apple step cannot be removed by the launcher. The IPA alone is not directly installable.
3. Put all three devices on the same Wi-Fi. Click **Start test** on the laptop. The verified 469 MiB model downloads once. In each phone app, tap **Scan laptop QR** and scan that phone's code on the laptop screen. Allow camera, local-network and (on Android) notification access when asked. Keep the iPhone app open on screen until the result appears; iOS disconnects it in the background. The Android app keeps computing if you lock that phone or switch apps, and shows a "ShareCompute is computing" notification with a **Disconnect** button. Click **Download report**.

If Windows asks about network access, permit the launcher on your private network. On Linux, an active firewall must allow incoming TCP 9443 from your local network. Guest Wi-Fi/client isolation can block the phones; use a network where devices can reach each other. VPNs can select the wrong laptop address: edit the address shown in the dashboard to your Wi-Fi IPv4 address.

## Every later test

Open the laptop launcher, click **Start test**, then scan/connect both phone apps. The model is reused. If the iPhone signature expired, refresh it in SideStore first. No development environment is required. Tap Connect again after a test disconnects.

## What PASS means

Each physical phone and the laptop completed native model computation with nonzero assigned layers, graph counters, memory allocations, and tunnel traffic; the 16 generated tokens matched a laptop-only baseline. The report records the proof. Phone identities are worker reports, not cryptographic hardware attestation.

This is a small-model splitting proof, not a claim that the devices expose one pooled RAM address space, can run any large model, or will be faster than the laptop alone. Native buffer budgets are 768 MiB / 2048 MiB / 1536 MiB; these are not whole-app RAM limits. Exact token matching on mixed CPU architectures can still fail, and a failure must be investigated rather than treated as a pass.

## Troubleshooting and removal

- Waiting for phones: check the Wi-Fi address, firewall, that the iPhone app is on screen, and that each app scanned its own QR. The join window is 15 minutes. Close the launcher with **Close launcher**, not merely by closing its browser tab.
- Camera unavailable: expand the dashboard's pairing-code field, copy it to the matching app, and tap Connect. Treat the QR/code as private credentials.
- The model is downloaded only on the laptop. Internet is needed for the first download and phone installation, not for the subsequent local test.
- Some Android brands stop background apps aggressively despite the notification. If the Android worker drops while locked, set its battery usage to unrestricted.
- These are test builds. A newly built Android APK may use a different test signing key; uninstall the previous test app first if Android refuses an update. Re-scan after reinstalling.
- Download report includes logs and the result, not pairing tokens or the laptop certificate's private key. Logs contain local paths and device IP addresses; review before sharing.
- Laptop data is isolated in `%LOCALAPPDATA%\ShareCompute` on Windows or `~/.local/share/ShareCompute` on Linux. To remove it, close the launcher and delete that folder plus the extracted kit. Uninstall the phone apps normally.
- Advanced build/manual setup: [THREE-DEVICE-MODEL-SPLIT.md](https://github.com/joeydd032995-pixel/ShareCompute/blob/feature/three-device-model-split/docs/THREE-DEVICE-MODEL-SPLIT.md).
