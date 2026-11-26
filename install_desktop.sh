#!/usr/bin/env bash
# Upgrade dsh-compaction-select in the desktop profile. Adapted from dsh-cite-sources' install_desktop.sh; the
# differences follow from how this package is wired: the standard-light preset loads it, so it is neither a
# dsh.profile.bundles entry nor a compatibility.json key, and only its "dependencies" spec changes.
#   1. tgz in $DSH_HOME/.local-packages/
#   2. profiles/desktop/package.json: the "dependencies" file: entry moves to the new tgz
#   3. pnpm install in profiles/desktop (updates pnpm-lock.yaml and node_modules; needs network), with the pnpm that
#      wrote the profile: node_modules/.modules.yaml records "packageManager"; the app's bundled runtime is used, PATH
#      pnpm only when it reports that same version
#   4. other dsh-* packages patched in place after their install are restored from the profile tarball if pnpm
#      rewrote them
# Upgrade only: the profile must already depend on $NAME through file:$LOCAL_PACKAGES/$NAME-<old>.tgz. The lockfile
# may change only in $NAME's own entries (file name, integrity, and its packages entry's `version:` line), the
# installed lib/index.js must match the new tgz, and the old tgz is removed only after success.
# Refuses to run while the app (or anything else) has DSH_HOME open. Backs up every file it changes, plus a tarball
# of the whole profile directory; on any failure after the backup it restores the profile directory from that tarball.
set -euo pipefail

DSH_HOME="${DSH_HOME:-$HOME/.local/share/deepseek-harness}"
PROFILE="$DSH_HOME/profiles/desktop"
LOCAL_PACKAGES="$DSH_HOME/.local-packages"
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME=dsh-compaction-select
VERSION="$(node -p "require('$PKG_DIR/package.json').version")"
TGZ="$LOCAL_PACKAGES/$NAME-$VERSION.tgz"
TS="$(date +%Y%m%d-%H%M)"
TAG="bak-pre-compaction-select-$TS"

die() { echo "install_desktop: $*" >&2; exit 1; }

# 1. The app must be fully closed: no DeepSeek Harness process, nothing holding files under DSH_HOME.
APP_PATTERN='Applications/DeepSeek-Harness/|deepseek-harness/dsh-runtimes/'
if pgrep -f "$APP_PATTERN" >/dev/null; then
  pgrep -af "$APP_PATTERN" | cut -c1-160 >&2
  die "DeepSeek Harness is running; quit it completely (including the tray) and retry"
fi
if command -v lsof >/dev/null && lsof +D "$DSH_HOME" 2>/dev/null | tail -n +2 | grep -q .; then
  lsof +D "$DSH_HOME" 2>/dev/null >&2 || true
  die "files under $DSH_HOME are open (listed above); close those processes and retry"
fi

[ -f "$PROFILE/package.json" ] || die "no profile at $PROFILE"

# The pnpm that wrote this profile.
WANT_PNPM="$(grep -o '"packageManager": "pnpm@[^"]*"' "$PROFILE/node_modules/.modules.yaml" | head -1 | sed 's/.*pnpm@//; s/"$//')"
[ -n "$WANT_PNPM" ] || die "cannot read packageManager from $PROFILE/node_modules/.modules.yaml"
APP_DIR="${DSH_APP_DIR:-$HOME/Applications/DeepSeek-Harness}"
PNPM=()
for runtime in "$DSH_HOME/dsh-runtimes/dsh-primary-runtime/dependencies" "$APP_DIR/resources/runtime/primary-runtime/dependencies"; do
  if [ -x "$runtime/node/bin/node" ] && [ -f "$runtime/pnpm/bin/pnpm.mjs" ] \
    && [ "$(cd /tmp && "$runtime/node/bin/node" "$runtime/pnpm/bin/pnpm.mjs" --version 2>/dev/null)" = "$WANT_PNPM" ]; then
    PNPM=("$runtime/node/bin/node" "$runtime/pnpm/bin/pnpm.mjs")
    break
  fi
done
if [ ${#PNPM[@]} -eq 0 ] && command -v pnpm >/dev/null && [ "$(cd /tmp && pnpm --version)" = "$WANT_PNPM" ]; then
  PNPM=(pnpm)
fi
[ ${#PNPM[@]} -gt 0 ] || die "no pnpm $WANT_PNPM found (app runtime under $DSH_HOME/dsh-runtimes or $APP_DIR, or PATH)"
echo "using pnpm $WANT_PNPM: ${PNPM[*]}"

# Network precheck: the selected pnpm's node must reach the npm registry.
NODE="${PNPM[0]}"
[ "$NODE" = pnpm ] && NODE="$(command -v node)"
[ -x "$NODE" ] || die "network precheck: cannot find a node binary (PNPM[0]=$NODE)"
"$NODE" -e "
  const c = new AbortController();
  const t = setTimeout(() => c.abort(), 10000);
  fetch('https://registry.npmjs.org/@deepseek-ai%2fcosmokit', { signal: c.signal })
    .then(r => { clearTimeout(t); process.exit(r.ok ? 0 : 1); })
    .catch(e => { clearTimeout(t); console.error('registry fetch failed:', e.message); process.exit(1); });
" || die "network precheck failed: could not fetch https://registry.npmjs.org/@deepseek-ai/cosmokit within 10 s; this install needs network, check connectivity and retry"

OLD_SPEC="$(node -p "require('$PROFILE/package.json').dependencies?.['$NAME'] ?? ''")"
case "$OLD_SPEC" in
  "file:$LOCAL_PACKAGES/$NAME-"*.tgz) ;;
  *) die "$PROFILE/package.json does not depend on $NAME as file:$LOCAL_PACKAGES/$NAME-<version>.tgz (found '$OLD_SPEC')" ;;
esac
OLD_TGZ="${OLD_SPEC#file:}"
OLD_FILE_VERSION="${OLD_TGZ##*/"$NAME"-}"
OLD_FILE_VERSION="${OLD_FILE_VERSION%.tgz}"
[ -f "$OLD_TGZ" ] || die "the installed dependency's tgz $OLD_TGZ is missing"
# The lockfile's `version:` line holds the package.json version inside the tgz, which a +<commit> file name omits.
OLD_PKG_VERSION="$(tar -xzOf "$OLD_TGZ" package/package.json | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>console.log(JSON.parse(s).version))')"
[ "$OLD_FILE_VERSION" != "$VERSION" ] && [ "$OLD_PKG_VERSION" != "$VERSION" ] \
  || die "$NAME@$VERSION is already installed ($OLD_TGZ); bump the version in $PKG_DIR/package.json"
echo "upgrading $NAME $OLD_FILE_VERSION (package version $OLD_PKG_VERSION) -> $VERSION"
grep -q "\"$NAME\"" <(node -p "JSON.stringify(require('$PROFILE/package.json').dsh?.profile?.bundles ?? [])") \
  && die "$NAME is listed in dsh.profile.bundles; this installer expects it loaded by the preset only"

# 2. Gate on the package's own tests (real session logs, no network).
(cd "$PKG_DIR" && npm test >/dev/null 2>&1) || die "npm test failed in $PKG_DIR"

# 3. Pack. Refuse to overwrite a different tgz of the same version.
PACK_DIR="$(mktemp -d)"
BACKED_UP=0
DONE=0
on_exit() {
  local status=$?
  rm -rf "$PACK_DIR"
  if [ "$BACKED_UP" = 1 ] && [ "$DONE" = 0 ]; then
    echo "install_desktop: failed; restoring $PROFILE from desktop.$TAG.tgz" >&2
    rm -rf "$PROFILE"
    tar -C "$DSH_HOME/profiles" -xzf "$DSH_HOME/profiles/desktop.$TAG.tgz"
    if [ -n "${NEW_TGZ:-}" ]; then rm -f "$TGZ"; fi
  fi
  exit "$status"
}
trap on_exit EXIT
(cd "$PKG_DIR" && npm pack --silent --pack-destination "$PACK_DIR" >/dev/null)
if [ -e "$TGZ" ] && ! cmp -s "$TGZ" "$PACK_DIR/$NAME-$VERSION.tgz"; then
  die "$TGZ exists with different content; bump the version"
fi

# 4. Back up every file this script changes, and the whole profile directory.
for f in package.json pnpm-lock.yaml; do
  [ -e "$PROFILE/$f" ] && cp -p "$PROFILE/$f" "$PROFILE/$f.$TAG"
done
tar -C "$DSH_HOME/profiles" -czf "$DSH_HOME/profiles/desktop.$TAG.tgz" desktop
echo "backups: $PROFILE/{package.json,pnpm-lock.yaml}.$TAG and $DSH_HOME/profiles/desktop.$TAG.tgz"

BACKED_UP=1

if [ ! -e "$TGZ" ]; then
  cp "$PACK_DIR/$NAME-$VERSION.tgz" "$TGZ"
  NEW_TGZ=1
fi

# 5. package.json: only the dependency spec changes (key order otherwise preserved).
node - "$PROFILE" "$NAME" "$TGZ" <<'EOF'
const fs = require("node:fs");
const [profile, name, tgz] = process.argv.slice(2);
const pkgPath = `${profile}/package.json`;
const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
pkg.dependencies[name] = `file:${tgz}`;
fs.writeFileSync(pkgPath, `${JSON.stringify(pkg, null, 2)}\n`);
EOF

# 6. Install. Packages that are real directories (not links) are fingerprinted first, because some were patched in
#    place after install and a reinstall would revert them. $NAME itself is the package being replaced.
fingerprint() { (cd "$PROFILE/node_modules" && find "$1" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-64); }
declare -A BEFORE
for dir in "$PROFILE"/node_modules/dsh-*; do
  [ -d "$dir" ] && [ ! -L "$dir" ] || continue
  pkg="$(basename "$dir")"
  [ "$pkg" != "$NAME" ] || continue
  BEFORE[$pkg]="$(fingerprint "$pkg")"
done

# Lockfile guard: the install must not change any locked package or importer dependency outside $NAME.
BEFORE_LOCK="$PACK_DIR/lockfile.before"
AFTER_LOCK="$PACK_DIR/lockfile.after"
python3 "$PKG_DIR/lockfile_guard.py" "$PROFILE/pnpm-lock.yaml" "$NAME" > "$BEFORE_LOCK" \
  || die "lockfile guard: cannot extract pinned set from $PROFILE/pnpm-lock.yaml"
(cd "$PROFILE" && "${PNPM[@]}" install)
for pkg in "${!BEFORE[@]}"; do
  if [ ! -d "$PROFILE/node_modules/$pkg" ] || [ "$(fingerprint "$pkg")" != "${BEFORE[$pkg]}" ]; then
    echo "pnpm rewrote node_modules/$pkg; restoring the pre-install copy from desktop.$TAG.tgz"
    rm -rf "$PROFILE/node_modules/$pkg"
    tar -C "$DSH_HOME/profiles" -xzf "$DSH_HOME/profiles/desktop.$TAG.tgz" "desktop/node_modules/$pkg"
    [ "$(fingerprint "$pkg")" = "${BEFORE[$pkg]}" ] || die "could not restore node_modules/$pkg"
  fi
done

python3 "$PKG_DIR/lockfile_guard.py" "$PROFILE/pnpm-lock.yaml" "$NAME" > "$AFTER_LOCK" \
  || die "lockfile guard: cannot re-extract pinned set from $PROFILE/pnpm-lock.yaml"
if ! cmp -s "$BEFORE_LOCK" "$AFTER_LOCK"; then
  echo "install_desktop: lockfile guard: locked packages or importer dependencies changed outside $NAME:" >&2
  diff -u "$BEFORE_LOCK" "$AFTER_LOCK" >&2 || true
  die "lockfile guard failed; restore path below reverts $PROFILE"
fi

# Upgrade lockfile guard: the new lockfile must equal the pre-install one with $NAME-$OLD_FILE_VERSION.tgz renamed to
# $NAME-$VERSION.tgz everywhere and, in $NAME's own packages entry only, the resolution integrity replaced by the new
# tgz's sha512 and `version: $OLD_PKG_VERSION` by `version: $VERSION`; any other change fails.
python3 - "$PROFILE/pnpm-lock.yaml.$TAG" "$PROFILE/pnpm-lock.yaml" "$NAME" "$OLD_FILE_VERSION" "$OLD_PKG_VERSION" "$VERSION" "$TGZ" <<'EOF' \
  || die "lockfile guard: pnpm-lock.yaml changed outside $NAME's own entries"
import base64, difflib, hashlib, re, sys
before_path, after_path, name, old_file_version, old_pkg_version, new, tgz = sys.argv[1:]
before = open(before_path, encoding="utf-8").read()
after = open(after_path, encoding="utf-8").read()
integrity = "sha512-" + base64.b64encode(hashlib.sha512(open(tgz, "rb").read()).digest()).decode()
old_file = re.escape(f"{name}-{old_file_version}.tgz")
# The packages entry: its key line (no peer suffix) and the indented lines below it, up to the blank line.
entry = re.compile(r"^  " + re.escape(name) + r"@file:[^\n(]*/" + old_file + r":\n(?:    [^\n]*\n)*", re.M)
found = entry.findall(before)
if len(found) != 1:
    sys.exit(f"lockfile guard: expected one {name}-{old_file_version}.tgz packages entry in the pre-install lockfile, found {len(found)}")
block, n_int = re.subn(r"(resolution: \{integrity: )sha512-[A-Za-z0-9+/=]+(, tarball: file:[^}]*/" + old_file + r"\})",
                       lambda m: m.group(1) + integrity + m.group(2), found[0])
block, n_ver = re.subn(r"^    version: " + re.escape(old_pkg_version) + r"$", f"    version: {new}", block, flags=re.M)
if n_int != 1 or n_ver != 1:
    sys.exit(f"lockfile guard: {name}'s entry has {n_int} resolution and {n_ver} version lines; expected one of each")
expected = before.replace(found[0], block).replace(f"{name}-{old_file_version}.tgz", f"{name}-{new}.tgz")
if after != expected:
    sys.stderr.writelines(difflib.unified_diff(expected.splitlines(True), after.splitlines(True), "expected", "pnpm-lock.yaml"))
    sys.exit(1)
EOF

[ -f "$PROFILE/node_modules/$NAME/lib/index.js" ] || die "pnpm did not install $NAME"
INSTALLED_VERSION="$(node -p "require('$PROFILE/node_modules/$NAME/package.json').version")"
[ "$INSTALLED_VERSION" = "$VERSION" ] || die "node_modules/$NAME is version $INSTALLED_VERSION, expected $VERSION"
WANT_LIB="$(tar -xzOf "$TGZ" package/lib/index.js | sha256sum | cut -c1-64)"
GOT_LIB="$(sha256sum < "$PROFILE/node_modules/$NAME/lib/index.js" | cut -c1-64)"
[ "$GOT_LIB" = "$WANT_LIB" ] || die "node_modules/$NAME/lib/index.js sha256 $GOT_LIB differs from the new tgz's $WANT_LIB"
SRC_LIB="$(sha256sum < "$PKG_DIR/lib/index.js" | cut -c1-64)"
[ "$GOT_LIB" = "$SRC_LIB" ] || die "node_modules/$NAME/lib/index.js differs from $PKG_DIR/lib/index.js"

# Post-install summary.
echo "=== post-install summary ==="
echo "package.json: $NAME -> $(node -p "require('$PROFILE/package.json').dependencies['$NAME']")"
grep -n "$NAME" "$PROFILE/pnpm-lock.yaml" | sed 's/^/lockfile: /'
echo "node_modules/$NAME: version $INSTALLED_VERSION, lib/index.js sha256 $GOT_LIB (== $PKG_DIR/lib/index.js)"
DONE=1
rm -f "$OLD_TGZ"
echo "removed $OLD_TGZ"
echo "installed $NAME@$VERSION into $PROFILE"
