#!/usr/bin/env python3
"""lockfile_guard.py -- extract the pinned set from a pnpm-lock.yaml.

Usage: lockfile_guard.py LOCKFILE [NAME ...]
  Prints one line per package entry / importer dependency, sorted:
    P <name>@<version>\t<integrity>
    I <importer> <dep>\t<version>\t[integrity]
  Lines for NAME (default: dsh-compaction-select) are excluded.
  Exit codes: 0 = extracted OK, 1 = parse error, 2 = file missing.
"""
import sys

try:
    import yaml
    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False

DEFAULT_EXCLUDE = "dsh-compaction-select"


def _split_dep(spec):
    # pnpm specs: "pkg@ver" | "pkg@ver(integ)" | "pkg@ver(integ) peerX@y" | "/pkg@ver"
    spec = spec.strip()
    if spec.startswith("/"):
        spec = spec[1:]
    if "(" in spec:
        i = spec.find("(")
        integ = spec[i + 1:-1]
        spec = spec[:i]
    else:
        integ = None
    spec = spec.split(" ")[0]
    name, ver = spec.rsplit("@", 1)
    return name, ver, integ


def extract_yaml(path, excludes):
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    lines = []
    for key in (data.get("packages") or {}):
        k = key[1:] if key.startswith("/") else key
        name, ver, _ = _split_dep(k)
        if name in excludes:
            continue
        integ = None
        ent = data["packages"][key] or {}
        if isinstance(ent, dict) and ent.get("resolution"):
            res = ent["resolution"]
            if isinstance(res, dict) and res.get("integrity"):
                integ = res["integrity"]
        lines.append((f"P {name}@{ver}", integ))
    for imp in (data.get("importers") or {}):
        imps = (data["importers"][imp] or {})
        for kind in ("dependencies", "devDependencies"):
            for dep, ver in (imps.get(kind) or {}).items():
                if dep in excludes:
                    continue
                name = dep
                integ = None
                ver_str = None
                if isinstance(ver, dict):
                    # importer dep entries are {specifier, version}
                    ver_str = ver.get("version", ver.get("specifier"))
                elif isinstance(ver, str) and "(" in ver:
                    name, _, integ = _split_dep(ver)
                    ver_str = ver
                else:
                    ver_str = str(ver)
                lines.append((f"I {imp} {name}", ver_str, integ))
    return lines


def extract_fallback(path, excludes):
    """Minimal parser for a pnpm-lock.yaml v9: '  /name@ver:' / '  name@ver:' package keys,
    '    integrity:' lines, and 'importers:' blocks with dependencies/devDependencies lists."""
    lines = []
    in_importers = False
    current_importer = None
    in_deps = False
    in_pkg = False
    pkg_name = None
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line == "importers:":
                in_importers = True
                in_pkg = False
                continue
            if in_importers:
                if line and not line.startswith(" "):
                    in_importers = False
                    continue
                if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
                    current_importer = line.strip()[:-1]
                    in_deps = False
                    continue
                if line.startswith("    ") and not line.startswith("      ") and line.rstrip().endswith(":"):
                    in_deps = line.strip()[:-1] in ("dependencies", "devDependencies")
                    continue
                if in_deps and line.startswith("      ") and ":" in line:
                    dep, ver = line.strip().split(":", 1)
                    dep = dep.strip().strip("'\"")
                    ver = ver.strip().strip("'\"")
                    if dep in excludes:
                        continue
                    name = dep
                    if isinstance(ver, str) and "(" in ver:
                        name, _, _ = _split_dep(ver)
                    lines.append((f"I {current_importer} {name}", ver))
                    continue
            if (not in_importers and line.startswith("  ") and not line.startswith("    ")
                    and line.rstrip().endswith(":")
                    and (line.strip().startswith("/") or "@" in line)):
                in_pkg = True
                pkg_name = _split_dep(line.strip()[:-1])[0:2]
                continue
            if in_pkg:
                if line.startswith("    integrity:"):
                    integ = line.split(":", 1)[1].strip().strip("'\"")
                    name, ver = pkg_name
                    if name not in excludes:
                        lines.append((f"P {name}@{ver}", integ))
                    in_pkg = False
                elif line.startswith("  ") and not line.startswith("    "):
                    in_pkg = False
    return lines


def main():
    args = sys.argv[1:]
    if not args:
        print("usage: lockfile_guard.py LOCKFILE [NAME ...]", file=sys.stderr)
        return 2
    path = args[0]
    excludes = set(args[1:] or [DEFAULT_EXCLUDE])
    try:
        lines = extract_yaml(path, excludes) if HAVE_YAML else extract_fallback(path, excludes)
    except FileNotFoundError:
        return 2
    except Exception as e:
        print(f"lockfile_guard: parse error: {e}", file=sys.stderr)
        return 1
    for item in sorted(lines, key=lambda x: tuple(str(v) for v in x)):
        print("\t".join("" if v is None else str(v) for v in item))
    return 0


if __name__ == "__main__":
    sys.exit(main())
