# A346E staged monitor-mode and injection design

Date: 2026-11-26

## Intent

Expose the existing MediaTek `gen4m_s1` receive-only sniffer as a standard cfg80211 monitor interface, add management-frame injection on that interface, and then add raw 802.11 data-frame injection as a separate tested stage.

The goal is a usable `iw`/`tcpdump` workflow on the phone without replacing the vendor driver or modifying firmware.

## Current verified state

- Device: Samsung SM-A346E, MediaTek MT6877.
- Running kernel: `6.6.144-android15-8-4k`.
- Source revision: `ae1adba67 update kernel 6.6.144_r01`.
- Source repository: `https://github.com/BDhackers009/Kernel_Samsung_a346E`, branch `A346E`.
- Build route: the repository's GitHub Actions workflow (`build_kernel.yml`), which has the AOSP/Kleaf prebuilts, Bazel, disk, and memory missing from the local Termux environment.
- Vendor WLAN source is present under `vendor/mediatek/kernel_modules/connectivity/wlan/core/gen4m_s1`.
- `CFG_SUPPORT_SNIFFER` is enabled.
- `priv_driver_set_monitor` sends `CMD_ID_SET_MONITOR` to firmware and schedules `wlanMonWorkHandler`.
- `wlanMonWorkHandler` creates `radiotap%d` with `ARPHRD_IEEE80211_RADIOTAP`.
- Live monitor capture has been verified with tcpdump.
- `wlan_mon_netdev_ops` currently has only open/stop handlers.
- cfg80211 does not advertise `NL80211_IFTYPE_MONITOR`.
- Raw management TX through `NL80211_CMD_FRAME` has been live-verified: the driver accepted a broadcast probe request and `iw event -f` reported `mgmt TX status ... acked`. This is driver/firmware completion evidence; independent over-the-air capture was not performed.
- `nic_txd_v2_compose` supports `fgIs802_11`, sets normal 802.11 header format, and reads type/subtype from the raw MAC header.
- HQA TX supports a configurable MAC header and generated/fixed payload, not an arbitrary payload buffer.

## Goals

1. `iw phy phy0 interface add mon0 type monitor` succeeds.
2. `ip link set mon0 up` exposes radiotap capture through the new interface.
3. Management frames written to `mon0` are transmitted through the existing firmware-backed management TX path.
4. Normal station, P2P, and AP operation remains intact when monitor mode is not active. The existing full-MAC sniffer dedicates RX to the monitor netdev while active, so normal data reception pauses until monitor is removed.
5. Stage 2 adds raw data-frame transmission using the existing raw-802.11 TX descriptor support.
6. Every runtime test has a documented rollback path.

## Non-goals

- Arbitrary control-frame injection such as hand-crafted ACK/CTS/RTS.
- Regulatory-domain bypass or operation outside legal channels.
- Firmware patching.
- Replacing the vendor driver with `mt76` or `mac80211`.
- Automatic flashing or permanent boot-image replacement.

## Constraints

- The WLAN device is full-MAC; firmware still controls the radio.
- cfg80211 uses a unified wiphy (`CFG_ENABLE_UNIFY_WIPHY=1`).
- The monitor netdev must be linked to a `wireless_dev` and registered through cfg80211 for standard `iw` support.
- Raw data TX requires valid BSS/STA records, queue selection, and TX descriptor fields.
- Module CRC/version compatibility must match the running kernel and build configuration.
- The current AnyKernel3 package uses `do.modules=0` and uploads only `Image`; it does not deploy a patched WLAN `.ko`.
- No patched module will be loaded or flashed without a separate explicit approval.
- Live `insmod`/`rmmod` of the active WLAN driver is not an approved test route because it risks duplicate symbols and a broken radio state.

## Approaches considered

### A. No-rebuild proc/netlink helper

Use `/proc/net/wlan/driver` for monitor and `NL80211_CMD_FRAME` for management injection.

- Pros: fastest, lowest device risk, already proven.
- Cons: not a standard cfg80211 monitor interface; no monitor-interface TX socket.

Rejected as the final implementation because the selected goal is standard `iw` integration.

### B. One-shot full radiotap injection patch

Add cfg80211 monitor plus raw data/management TX in one change.

- Pros: shortest path to a complete feature set.
- Cons: highest crash/debug surface; combines cfg80211 lifecycle, radiotap parsing, BSS/STA selection, and firmware TX debugging.

Rejected because it makes failures hard to isolate.

### C. Staged driver patch — selected

Stage 1 exposes cfg80211 monitor and management TX. Stage 2 adds raw data TX after Stage 1 is stable.

- Pros: isolates lifecycle and capture bugs from TX descriptor bugs; reuses proven management TX.
- Cons: takes longer and temporarily supports management injection only.

## Stage 1 design

### 1. Advertise monitor mode

In `os/linux/gl_init.c`:

- Add `BIT(NL80211_IFTYPE_MONITOR)` to `prWiphy->interface_modes`.
- Update the unified-wiphy interface combinations to permit one monitor interface without violating existing station/P2P limits.

### 2. Refactor monitor netdev creation

Extract the allocation/registration logic from `wlanMonWorkHandler` into a helper that can:

- accept a name and `struct wireless_dev *`,
- allocate the radiotap netdev,
- attach `ndev->ieee80211_ptr`,
- set `wdev->wiphy`, `wdev->netdev`, and `wdev->iftype`,
- register through `cfg80211_register_netdevice`,
- retain the existing `ARPHRD_IEEE80211_RADIOTAP` setup.

The existing proc command remains available as a compatibility path.

### 3. Add cfg80211 monitor lifecycle

In `os/linux/gl_cfg80211.c`:

- `mtk_cfg_add_iface`: detect `NL80211_IFTYPE_MONITOR`, allocate a wdev, create the monitor netdev, and enable firmware monitor mode on the current/default legal channel.
- `mtk_cfg_del_iface`: disable firmware monitor mode, unregister the netdev, and free the wdev.
- `mtk_cfg_change_iface`: support transitions into/out of monitor if safe; otherwise return `EOPNOTSUPP` rather than partially changing state.

Factor the channel/bandwidth preparation from `priv_driver_set_monitor` into a shared helper so proc and cfg80211 use identical validation.

### 4. Add management-frame TX on monitor

Add `wlanMonXmit` to `wlan_mon_netdev_ops`:

- validate and strip the radiotap header,
- require a management frame in Stage 1,
- allocate `MSG_MGMT_TX_REQUEST` and `cnmMgtPktAlloc`,
- copy the raw 802.11 frame,
- set `MID_MNY_AIS_MGMT_TX`, BSS index, cookie, and channel fields,
- send through the same mailbox path used by `_mtk_cfg80211_mgmt_tx`,
- consume the skb on success or drop it cleanly on failure.

Factor the common allocation/copy/mailbox code from `_mtk_cfg80211_mgmt_tx` where practical to avoid divergent behavior.

Stage 1 drops non-management frames and increments the monitor netdev TX-drop counter. A netdev `ndo_start_xmit` callback cannot return `EOPNOTSUPP` to userspace.

### 5. Implementation notes

- `wlanMonitorSetFirmware()` uses the current AIS primary channel and a conservative 20 MHz `CHNL_EXT_SCN` configuration. The existing proc command retains its advanced channel/bandwidth parser, but is rejected while a cfg80211 monitor is active so it cannot tear down a cfg80211-owned netdev behind cfg80211's back.
- `wlanMonXmit()` validates the radiotap length and 802.11 frame type, queues the skb, and processes it in a workqueue. The worker calls the existing `mtk_cfg80211_mgmt_tx()` path rather than duplicating management allocation/mailbox logic.
- Monitor teardown first marks TX not-ready, stops queues, synchronizes the worker, and purges queued skbs. Firmware monitor mode is disabled only after TX is quiescent. A firmware-disable error leaves the cfg80211 interface intact and returns `-EIO` so deletion can be retried.
- Successful teardown uses `cfg80211_unregister_netdevice()` when called from an RTNL-held cfg80211 callback. The netdev destructor is installed only after registration succeeds, avoiding registration-failure double frees.
- Monitor creation/destruction is serialized in RTNL -> lifecycle-mutex -> TX-queue-lock order. Legacy proc commands hold the lifecycle mutex through firmware configuration and work scheduling; driver removal marks the monitor lifecycle as stopping before canceling work.
- RX takes a temporary monitor netdev reference under the monitor state lock, consumes the skb on failure, and bypasses normal NAPI/GRO for radiotap packets.
- AIS management/remain-on-channel cookies use an atomic 64-bit counter so monitor-worker TX cannot duplicate cfg80211 cookies.
- Transitions into or out of monitor through `NL80211_CMD_SET_INTERFACE` are intentionally rejected; users must add/delete the monitor interface. Unsupported monitor flags at creation or in later flag-change requests return `EOPNOTSUPP`.
- The WLAN artifact contains the module, matching `Image`, available direct connectivity dependency modules, SHA-256 checksums, `.modinfo`, `__versions`, symbol/dependency metadata, and source commit.

## Stage 2 design

Add raw data-frame TX after Stage 1 passes runtime testing.

### 1. Radiotap TX parsing

Parse radiotap version/length/present fields and extract:

- rate or MCS,
- bandwidth,
- short GI,
- STBC/LDPC flags where supported,
- channel/frequency when supplied.

Unsupported fields are ignored only when safe; ambiguous rate/bandwidth combinations are rejected.

### 2. Raw MSDU construction

Add a helper in `nic/nic_tx.c` that fills an `MSDU_INFO` for an already-formed 802.11 frame:

- `prPacket` with `MAC_TX_RESERVED_FIELD` headroom,
- raw frame bytes and length,
- `fgIs802_11=TRUE`,
- 24-byte or 26-byte MAC header length,
- BSS index from the monitor interface context,
- STA record index from destination address, broadcast index when needed,
- data packet type, TID/TC, WMM queue set, rate mode, and completion handler.

Reuse `nic_txd_v2_compose` for type/subtype/header formatting and `nicTxEnqueueMsdu` for queueing.

### 3. Rate and queue mapping

Map radiotap rate information to the driver fixed-rate option and choose a valid TC/WMM queue. Start with conservative non-aggregated, no-AMSDU settings.

### 4. Control frames

Return `EOPNOTSUPP` for control frames unless a later firmware test proves support.

## Build and verification plan

### Build-only phase — GitHub Actions

1. Create a feature branch in `BDhackers009/Kernel_Samsung_a346E`.
2. Push the Stage 1 patch and let `.github/workflows/build_kernel.yml` build with the existing Ubuntu/Kleaf/AOSP toolchain.
3. Extend the workflow to locate and upload `wlan_drv_gen4m_6877.ko`, its SHA-256, module metadata, and the matching kernel `Image` as separate artifacts.
4. Run `modpost`, symbol CRC, vermagic, dependency, and compiler checks.
5. Compare the resulting module metadata with the currently loaded module.
6. Extract and compare the `__versions` symbol CRC tables for the WLAN module and its connectivity dependencies. If they match the running set, runtime testing may overlay only the WLAN module; otherwise CI must produce a matched kernel/connectivity-module bundle and no mixed-version live load is allowed.
7. Do not flash, bind-mount, or load the module during this phase.

The local Termux environment is used for source work and inspection only; it lacks `aosp-kernel/prebuilts`, Bazel, and sufficient free RAM for the full Kleaf build.

### Deployment route for runtime testing — requires separate approval

Recommended route: a reversible KernelSU test module.

- CI uploads the patched `.ko` as a separate artifact.
- A small KernelSU module contains the `.ko` and a `post-fs-data.sh` bind-mount script.
- The script bind-mounts the patched file over `/vendor/lib/modules/wlan_drv_gen4m_6877.ko` before the vendor Wi-Fi driver is loaded.
- The stock vendor file is never modified.
- Disabling the KernelSU module and rebooting restores the stock module path.
- No live `insmod`, `rmmod`, or emergency radio unload is used.

Fallback routes, only if explicitly approved:

- Install a KernelSU metamodule and use a vendor OverlayFS/magic-mount module.
- Temporarily replace the vendor module with a recovery-level backup/restore procedure.
- Boot a test kernel/ramdisk that loads the patched module from a controlled path.

### Stage 1 runtime phase

1. Preserve the stock kernel, module, and boot path.
2. Install the KernelSU test module in disabled state.
3. Reboot with the module enabled and confirm the patched module is the one loaded.
4. Verify normal Wi-Fi, P2P, and AP functionality.
5. Add/remove `mon0` repeatedly.
6. Capture frames with tcpdump.
7. Send one broadcast probe request through `mon0`.
8. Confirm TX completion through `iw event -f`.
9. Disable/remove monitor and verify Wi-Fi reconnects.

### Stage 2 runtime phase

1. Send a benign raw data frame on the configured channel.
2. Verify firmware TX completion and, where possible, reception by a second device.
3. Test rate/channel changes and queue behavior.
4. Remove monitor and verify normal operation.

## Rollback

- Keep the stock kernel, boot image, and WLAN module unchanged until runtime approval.
- Disable the KernelSU test module and reboot to restore the stock driver.
- Provide a monitor-disable/Wi-Fi-enable script for test sessions.
- If the patched module prevents Wi-Fi initialization, reboot with the KernelSU module disabled or into the preserved stock kernel.
- Do not attempt emergency module unloads on a broken radio path.

## Risks

- cfg80211 may reject monitor in the existing interface-combination validation.
- Incorrect wdev/netdev linkage can cause registration or teardown bugs.
- Raw data frames may require STA/BSS state not present while monitoring.
- Firmware may reject some raw frame types or rate combinations.
- The existing sniffer redirects all RX to the monitor netdev; concurrent station/P2P/AP data reception is not supported in Stage 1.
- Stage 1 programs the current AIS channel at 20 MHz. Changing the monitor channel through cfg80211 is not implemented yet.
- Source/toolchain mismatch can produce a module that builds but cannot load.

## Files expected to change

- `vendor/mediatek/kernel_modules/connectivity/wlan/core/gen4m_s1/os/linux/gl_init.c`
- `vendor/mediatek/kernel_modules/connectivity/wlan/core/gen4m_s1/os/linux/gl_cfg80211.c`
- `vendor/mediatek/kernel_modules/connectivity/wlan/core/gen4m_s1/os/linux/gl_p2p.c`
- `vendor/mediatek/kernel_modules/connectivity/wlan/core/gen4m_s1/os/linux/gl_wext_priv.c`
- corresponding headers under `os/linux/include/`
- `.github/workflows/build_kernel.yml` for the separate WLAN module artifact

Stage 2 is expected to add `nic/nic_tx.c` and the corresponding NIC headers.
