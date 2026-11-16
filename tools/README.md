# tools

## kabi-check.py — prove the new Image still accepts stock vendor modules

The phone runs Samsung's prebuilt modules (`/vendor/lib/modules`,
`/vendor_dlkm/lib/modules`, `/system_dlkm/lib/modules`) against this kernel.
Module loading is checked by modversions **CRCs**, not by the kernel version
string, so a config or source change that moves a struct a module depends on
gets the whole module refused and the phone bootloops.

Two ways to use it.

### Once: generate a baseline from the phone, commit it

```bash
adb pull /vendor/lib/modules stock-modules
adb pull /vendor_dlkm/lib/modules stock-modules/vendor_dlkm
adb pull /system_dlkm/lib/modules stock-modules/system_dlkm   # may be a symlink tree

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
