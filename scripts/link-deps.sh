#!/usr/bin/env bash
# Link every bare package that lib/index.js imports into this fork's own
# node_modules, pointing at the copies bundled with the global dsh install.
#
# Node resolves the fork by its realpath (~/dsh-compaction-select), so its
# direct imports must live in ./node_modules. Transitive dependencies resolve
# from each link's realpath inside the dsh install, so they need no links.
#
# Re-run after any dsh upgrade. Read-only with respect to the dsh install.
#
#   scripts/link-deps.sh            # link + verify
#   DSH_ROOT=/path/to/dsh scripts/link-deps.sh
set -euo pipefail

FORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENTRY="$FORK_DIR/lib/index.js"
DSH_ROOT="${DSH_ROOT:-$(npm root -g)/@deepseek-ai/dsh}"
DSH_NM="$DSH_ROOT/node_modules"
PROFILE_DIR="${PROFILE_DIR:-$HOME/.dsh/profiles/web}"

[[ -f "$ENTRY" ]] || { echo "missing $ENTRY" >&2; exit 1; }
[[ -d "$DSH_NM" ]] || { echo "missing dsh node_modules: $DSH_NM" >&2; exit 1; }

# Bare specifiers from static `import/export ... from "x"`, side-effect
# `import "x"` and dynamic `import("x")`; reduced to package names.
mapfile -t PKGS < <(
	grep -oE "(from|import)[[:space:]]*\(?[[:space:]]*[\"'][^\"']+[\"']" "$ENTRY" |
		sed -E "s/.*[\"']([^\"']+)[\"']/\1/" |
		grep -vE '^(\.|/|node:)' |
		awk -F/ '{ print ($1 ~ /^@/) ? $1"/"$2 : $1 }' |
		sort -u
)
[[ ${#PKGS[@]} -gt 0 ]] || { echo "no bare imports found in $ENTRY" >&2; exit 1; }

status=0
for pkg in "${PKGS[@]}"; do
	target="$DSH_NM/$pkg"
	if [[ ! -f "$target/package.json" ]]; then
		echo "MISSING  $pkg (not found at $target)" >&2
		status=1
		continue
	fi
	link="$FORK_DIR/node_modules/$pkg"
	mkdir -p "$(dirname "$link")"
	ln -sfn "$target" "$link"
	echo "linked   $pkg -> $target ($(node -p "require('$target/package.json').version"))"
done

# Report links left dangling by an upgrade (not removed automatically).
while IFS= read -r l; do
	echo "DANGLING $l -> $(readlink "$l")" >&2
done < <(find "$FORK_DIR/node_modules" -maxdepth 2 -xtype l)

[[ $status -eq 0 ]] || exit $status

# Verify exactly the way dsh loads the plugin: from the profile.
if [[ -d "$PROFILE_DIR" ]]; then
	(cd "$PROFILE_DIR" && node -e "import('dsh-compaction-select').then(()=>console.log('IMPORT OK'),e=>{console.log('FAIL',e.message);process.exit(1)})")
else
	(cd "$FORK_DIR" && node -e "import('./lib/index.js').then(()=>console.log('IMPORT OK'),e=>{console.log('FAIL',e.message);process.exit(1)})")
fi
