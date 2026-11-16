#!/usr/bin/env python3
"""Compare the symbol CRCs a stock Android module expects with the ones a new
kernel build exports.

Why this exists: the phone runs Samsung's prebuilt vendor modules against a
custom Image. Compatibility is checked by modversions CRCs, not by the version
string (the release string is ignored when CRCs are present). If any exported
symbol a module imports has a different CRC after a config change, the module
is refused at load time and the phone bootloops.

Usage:
    python3 tools/kabi-check.py --symvers debug/vmlinux.symvers --modules ~/stock-modules

`--symvers` is the vmlinux.symvers produced by the build (debug artifact).
`--modules` is a directory containing .ko files pulled from the phone, e.g.
    adb pull /vendor/lib/modules stock-modules
    adb pull /system_dlkm/lib/modules stock-modules/dlkm

Exit code 0 when every imported symbol matches, 1 otherwise.
"""

import argparse
import os
import struct
import sys

MODULE_NAME_LEN = 56  # include/linux/module.h


def read_symvers(path):
    """vmlinux.symvers: 0x<crc>\t<symbol>\t<module>"""
    crcs = {}
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                parts = line.split()
            if len(parts) < 2:
                continue
            try:
                crc = int(parts[0], 16)
            except ValueError:
                continue
            crcs[parts[1]] = crc
    return crcs


def _elf_sections(data):
    """Return (sections, name_of_section) for an ELF buffer."""
    if len(data) < 64 or data[:4] != b"\x7fELF":
        raise ValueError("not an ELF file")

    is64 = data[4] == 2
    if is64:
        shoff, = struct.unpack_from("<Q", data, 0x28)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x3A)
        sh_fmt = "<IIQQQQIIQQ"
    else:
        shoff, = struct.unpack_from("<I", data, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x2E)
        sh_fmt = "<IIIIIIIIII"

    if shoff == 0 or shnum == 0:
        return [], lambda _sh: ""

    sections = []
    for i in range(shnum):
        off = shoff + i * shentsize
        if off + struct.calcsize(sh_fmt) > len(data):
            break
        sections.append(struct.unpack_from(sh_fmt, data, off))

    names = b""
    if shstrndx < len(sections):
        sh = sections[shstrndx]
        names = data[sh[4]:sh[4] + sh[5]]

    def sec_name(sh):
        start = sh[0]
        end = names.find(b"\x00", start)
        return names[start:end].decode("ascii", "replace") if start < len(names) else ""

    return sections, sec_name


def elf_versions(path):
    """Return [(symbol, crc)] from a module's __versions section."""
    with open(path, "rb") as fh:
        data = fh.read()
    sections, sec_name = _elf_sections(data)
    is64 = len(data) > 4 and data[4] == 2
    crc_fmt = "<Q" if is64 else "<I"

    out = []
    for sh in sections:
        if sec_name(sh) != "__versions":
            continue
        offset, size = sh[4], sh[5]
        ent = struct.calcsize(crc_fmt) + MODULE_NAME_LEN
        for pos in range(0, size - ent + 1, ent):
            base = offset + pos
            (crc,) = struct.unpack_from(crc_fmt, data, base)
            raw = data[base + struct.calcsize(crc_fmt):base + ent]
            name = raw.split(b"\x00", 1)[0].decode("ascii", "replace")
            if name:
                out.append((name, crc))
        break
    return out


def elf_exported_names(path):
    """Names a module exports (__ksymtab_strings), so imports from another
    module are not mistaken for symbols the kernel is supposed to provide."""
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        sections, sec_name = _elf_sections(data)
    except ValueError:
        return set()
    for sh in sections:
        if sec_name(sh) == "__ksymtab_strings":
            blob = data[sh[4]:sh[4] + sh[5]]
            return {s.decode("ascii", "replace") for s in blob.split(b"\x00") if s}
    return set()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symvers", required=True, help="vmlinux.symvers from the new build")
    ap.add_argument("--modules", required=True, help="directory with stock .ko files")
    ap.add_argument("--allow-skip", action="store_true",
                    help="do not fail when a .ko cannot be parsed or has no __versions")
    args = ap.parse_args()

    if not os.path.isfile(args.symvers):
        print(f"error: {args.symvers} not found", file=sys.stderr)
        return 2
    if not os.path.isdir(args.modules):
        print(f"error: {args.modules} is not a directory", file=sys.stderr)
        return 2

    kernel = read_symvers(args.symvers)
    if not kernel:
        print("error: no symbols parsed from symvers", file=sys.stderr)
        return 2
    print(f"kernel exports: {len(kernel)} symbols")

    modules = []
    for root, _dirs, files in os.walk(args.modules):
        for f in files:
            if f.endswith(".ko"):
                modules.append(os.path.join(root, f))
    if not modules:
        print("error: no .ko files found", file=sys.stderr)
        return 2

    bad, missing, checked = [], [], 0
    skipped = []
    compared_modules = 0

    # Symbols that the modules themselves export (cfg80211 from mac80211 and so
    # on) are not supposed to be in vmlinux.symvers; a kernel config change
    # cannot move their CRCs either, so they are skipped rather than reported.
    module_exports = set()
    for path in modules:
        module_exports |= elf_exported_names(path)

    for path in sorted(modules):
        rel = os.path.relpath(path, args.modules)
        try:
            vers = elf_versions(path)
        except Exception as exc:  # noqa: BLE001 - report and continue
            skipped.append((rel, str(exc)))
            continue
        if not vers:
            skipped.append((rel, "no __versions section"))
            continue
        compared_modules += 1
        for name, crc in vers:
            if name not in kernel:
                if name not in module_exports:
                    missing.append((rel, name))
                continue
            checked += 1
            if kernel[name] != crc:
                bad.append((rel, name, crc, kernel[name]))

    for rel, name in missing[:20]:
        print(f"missing  {rel}: {name} (not exported by the new kernel)")
    if len(missing) > 20:
        print(f"missing  ... and {len(missing) - 20} more")

    for rel, name, want, got in bad:
        print(f"MISMATCH {rel}: {name} module=0x{want:08x} kernel=0x{got:08x}")

    if compared_modules == 0:
        print("error: no module could be compared - the module directory is empty or all files failed to parse",
              file=sys.stderr)
        return 2

    if skipped:
        for rel, why in skipped[:20]:
            print(f"skipped  {rel}: {why}", file=sys.stderr)
        if len(skipped) > 20:
            print(f"skipped  ... and {len(skipped) - 20} more", file=sys.stderr)
        if not args.allow_skip:
            print(f"\nFAILED: {len(skipped)} module(s) were not checked. Pull the complete module set, "
                  f"or pass --allow-skip if that is intentional.", file=sys.stderr)
            return 1

    if not bad and not missing:
        print(f"OK: {checked} imported symbol CRCs verified across {compared_modules} modules")
        return 0

    print(f"\nFAILED: {len(bad)} CRC mismatch(es), {len(missing)} missing symbol(s).")
    print("Do not flash this kernel: those modules will be refused at load time.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
