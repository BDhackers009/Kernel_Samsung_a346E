# tools

## kabi-check.py — prove the new Image still accepts stock vendor modules

The phone runs Samsung's prebuilt modules (`/vendor/lib/modules`,
`/vendor_dlkm/lib/modules`, `/system_dlkm/lib/modules`) against this kernel.
Module loading is checked by modversions **CRCs**, not by the kernel version
string, so a config or source change that moves a struct a module depends on
gets the whole module refused and the phone bootloops.

Two ways to use it.

### Once: generate a baseline from the phone, commit it

`/vendor/lib/modules` is a **symlink** to `/vendor_dlkm/lib/modules`; `adb pull`
of a symlinked directory copies nothing. Pull the real directories:

```bash
adb pull /vendor_dlkm/lib/modules stock-modules/vendor_dlkm
adb pull /system_dlkm/lib/modules stock-modules/system_dlkm
```

That covers ~260 of the ~456 loaded modules. The rest are packed in the
`vendor_boot` ramdisk (LZ4 legacy compressed cpio) and are loaded during early
boot. To include them:

```bash
adb shell "su -c 'dd if=/dev/block/by-name/vendor_boot of=/data/local/tmp/vb.img bs=1M'"
adb pull /data/local/tmp/vb.img vendor_boot.img
python3 - <<'EOF'
import lz4.block, os, struct
d = open("vendor_boot.img", "rb").read()
pos = d.find(b"\x02\x21\x4c\x18") + 4          # LZ4 legacy magic
out = bytearray()
while pos + 4 <= len(d):
    n = struct.unpack_from("<I", d, pos)[0]
    if not n or n > 8 << 20:
        break
    out += lz4.block.decompress(d[pos + 4:pos + 4 + n], uncompressed_size=8 << 20)
    pos += 4 + n
os.makedirs("stock-modules/vendor_boot", exist_ok=True)
data, pos = bytes(out), bytes(out).find(b"070701")
while data[pos:pos + 6] in (b"070701", b"070702"):
    f = [int(data[pos + 6 + i * 8:pos + 14 + i * 8], 16) for i in range(13)]
    name = data[pos + 110:pos + 110 + f[11] - 1].decode()
    off = pos + 110 + f[11]; off += (-off) % 4
    if name == "TRAILER!!!":
        break
    if name.endswith(".ko"):
        open(os.path.join("stock-modules/vendor_boot", os.path.basename(name)), "wb").write(data[off:off + f[6]])
    pos = off + f[6]; pos += (-pos) % 4
EOF
# needs: pip install lz4
python3 tools/kabi-check.py --dump-baseline stock-modules > abi/stock-modules.baseline
```

`abi/stock-modules.baseline` is the list of `(symbol, crc)` every stock module
imports. Once it is committed, the CI step **kABI check against stock modules**
runs on every `droidspaces` build and fails the run before the zip is packaged.
Do not regenerate it casually: it is the reference the kernel must keep matching.

### Every build: check a fresh Image against the baseline

```bash
# download the debug-<run_id> artifact (contains vmlinux.symvers)
python3 tools/kabi-check.py --symvers vmlinux.symvers --baseline abi/stock-modules.baseline
```

Exit code 0 means every symbol still has the CRC the modules were built
against. Anything else means do not flash.

You can also compare against a directory of `.ko` files directly, without a
baseline: `--modules stock-modules`.

Notes:

* Only `__versions` entries are read; modules built without `MODVERSIONS` have
  no CRCs. With `--modules`, a module that cannot be parsed makes the check fail
  by default — a partial module set must not report success. Pass `--allow-skip`
  if you knowingly pulled only part of it.
* Symbols the new kernel does not export at all are reported, because a missing
  symbol fails a module just as hard as a changed CRC. Symbols exported by
  *another module* (cfg80211 from mac80211, …) are recognised through
  `__ksymtab_strings` and skipped: the kernel change cannot affect them.
* The comparison is blind to *new* exported symbols; adding symbols is safe.
