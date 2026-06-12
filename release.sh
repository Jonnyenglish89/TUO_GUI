#!/usr/bin/env bash
# release.sh — commit everything, push, tag, and trigger the exe build.
#
# Usage (from WSL):
#   cd "/mnt/c/TUO GUI"
#   ./release.sh "what changed"            -> bumps the patch version (v1.0.0 -> v1.0.1)
#   ./release.sh "what changed" v2.0.0     -> uses the exact version given
#
# The pushed tag triggers .github/workflows/build-exe.yml, which builds
# TUO_GUI.exe on a clean Windows runner and attaches it to the release.

set -euo pipefail
cd "$(dirname "$0")"

MSG="${1:-}"
VERSION="${2:-}"

if [[ -z "$MSG" ]]; then
    read -rp "Commit message: " MSG
fi
if [[ -z "$MSG" ]]; then
    echo "Aborted: a commit message is required."
    exit 1
fi

# ── Safety: never let personal data slip into the repo ─────────────────────
if git status --porcelain | grep -qE "tuo_gui_data|cookie_|ownedcards"; then
    echo "ABORTED: personal data (cookies/inventories) is staged or untracked-visible."
    echo "Check .gitignore before releasing. Offending files:"
    git status --porcelain | grep -E "tuo_gui_data|cookie_|ownedcards"
    exit 1
fi

# ── Work out the next version ───────────────────────────────────────────────
LATEST=$(git tag --list 'v*' --sort=-v:refname | head -n 1)
if [[ -z "$VERSION" ]]; then
    if [[ -z "$LATEST" ]]; then
        VERSION="v1.0.0"
    else
        IFS=. read -r MAJOR MINOR PATCH <<< "${LATEST#v}"
        VERSION="v${MAJOR}.${MINOR}.$((PATCH + 1))"
    fi
fi
[[ "$VERSION" == v* ]] || VERSION="v${VERSION}"

if git rev-parse "$VERSION" >/dev/null 2>&1; then
    echo "ABORTED: tag $VERSION already exists. Pass a new version as the 2nd argument."
    exit 1
fi

echo "Previous version: ${LATEST:-none}"
echo "New version:      $VERSION"
echo "Commit message:   $MSG"
read -rp "Continue? [y/N] " CONFIRM
[[ "$CONFIRM" == [yY]* ]] || { echo "Aborted."; exit 1; }

# ── Commit, push, tag ────────────────────────────────────────────────────────
git add -A
if git diff --cached --quiet; then
    echo "No file changes — tagging the current commit."
else
    git commit -m "$MSG"
fi
git push origin main

git tag -a "$VERSION" -m "$MSG"
git push origin "$VERSION"

REPO_URL=$(git remote get-url origin | sed -e 's/\.git$//' -e 's#^git@github\.com:#https://github.com/#')
echo
echo "Done. $VERSION is building now:"
echo "  watch:    $REPO_URL/actions"
echo "  release:  $REPO_URL/releases/tag/$VERSION  (exe appears when the build finishes, ~3-5 min)"
