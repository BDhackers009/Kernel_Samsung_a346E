# tools

## kabi-check.py — prove the new Image still accepts stock vendor modules

The phone runs Samsung's prebuilt modules (`/vendor/lib/modules`,
`/vendor_dlkm/lib/modules`, `/system_dlkm/lib/modules`) against this kernel.
Module loading is checked by modversions **CRCs**, not by the kernel version
string, so a config or source change that moves a struct a module depends on
gets the whole module refused and the phone bootloops.

Run this between "CI finished" and "flash":

```bash
# 1. download the debug artifact from the workflow run (debug-<run_id>.zip)
#    it contains vmlinux.symvers and kernel.config
# 2. pull the stock modules once
adb pull /vendor/lib/modules stock-modules
adb pull /vendor_dlkm/lib/modules stock-modules/vendor_dlkm
adb pull /system_dlkm/lib/modules stock-modules/system_dlkm   # may be a symlink tree

# 3. compare
python3 tools/kabi-check.py --symvers vmlinux.symvers --modules stock-modules
```

Exit code 0 means every symbol those modules import still has the CRC they were
built against. Anything else means do not flash.

Notes:

* Only `__versions` entries are read; modules built without `MODVERSIONS` have
  no CRCs. A module that cannot be parsed, or has no `__versions`, makes the
  check fail by default — a partial module set must not report success. Pass
  `--allow-skip` if you knowingly pulled only part of it.
* Symbols the new kernel does not export at all are also reported, because a
  missing symbol fails a module just as hard as a changed CRC. Symbols exported
  by *another module* (cfg80211 from mac80211, …) are recognised through
  `__ksymtab_strings` and skipped: the kernel change cannot affect them.
* The comparison is blind to *new* exported symbols; adding symbols is safe.
