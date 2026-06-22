#!/usr/bin/env bash
# install.sh — set up cowork so it runs from any folder.
#
# Creates a dedicated virtualenv for the interactive UX deps, makes the
# `cowork` launcher available on your PATH (via ~/.zshrc), and verifies the
# controller CLIs. Idempotent — safe to re-run.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$APP_DIR/.venv"
VENV_PY="$VENV_DIR/bin/python"
LOCAL_SKILLS_DIR="$APP_DIR/skills"
COWORK_HOME="$HOME/.cowork"
COWORK_SESSIONS_DIR="$COWORK_HOME/sessions"
ZSHRC="$HOME/.zshrc"
PATH_MARKER_OPEN="# >>> cowork PATH >>>"
PATH_MARKER_CLOSE="# <<< cowork PATH <<<"

# venv python is >= 3.9?
venv_ok() {
    [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null
}

info()  { printf '  %s\n' "$*"; }
ok()    { printf '✓ %s\n' "$*"; }
warn()  { printf '! %s\n' "$*"; }
fail()  { printf '✗ %s\n' "$*" >&2; exit 1; }

# 1. Resolve python3 and assert >= 3.9 (matches cowork preflight floor).
command -v python3 >/dev/null 2>&1 || fail "python3 not found on PATH."
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    fail "Python 3.9+ required; found $(python3 -V 2>&1)."
fi
ok "Python $(python3 -V 2>&1 | awk '{print $2}')"

# 2. Create the venv if absent or stale (reuse only a healthy >=3.9 one).
if venv_ok; then
    info "Reusing existing venv at .venv"
else
    if [ -e "$VENV_DIR" ]; then
        info "Existing .venv is stale or pre-3.9 — rebuilding"
        rm -rf "$VENV_DIR"
    else
        info "Creating venv at .venv"
    fi
    if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
        fail "Could not create a virtualenv. The 'venv' module is missing — on Debian/Ubuntu install it with: sudo apt install python3-venv"
    fi
fi

# 3. Install the interactive UX deps into the venv.
info "Installing deps (rich, prompt_toolkit, questionary)"
"$VENV_PY" -m pip install --upgrade --quiet pip
"$VENV_PY" -m pip install --quiet -r "$APP_DIR/requirements.txt"
ok "Deps installed"

# 4. Make the launcher executable.
chmod +x "$APP_DIR/cowork"

# 5. Create the cowork home + sessions dir (idempotent).
mkdir -p "$COWORK_SESSIONS_DIR"
ok "cowork home ready at ~/.cowork (sessions/)"

# 6. Link bundled skills into Claude and Codex user skill directories.
if [ -d "$LOCAL_SKILLS_DIR" ]; then
    linked_count=0
    for user_skill_dir in "$HOME/.claude/skills" "$HOME/.codex/skills"; do
        mkdir -p "$user_skill_dir"
        for source in "$LOCAL_SKILLS_DIR"/*; do
            [ -d "$source" ] || continue
            [ -f "$source/SKILL.md" ] || {
                warn "Skipping $(basename "$source"): missing SKILL.md"
                continue
            }

            dest="$user_skill_dir/$(basename "$source")"
            if [ -L "$dest" ]; then
                if [ "$(readlink "$dest")" = "$source" ]; then
                    ok "$(basename "$source") already linked in $user_skill_dir"
                else
                    rm "$dest"
                    ln -s "$source" "$dest"
                    ok "Updated $(basename "$source") link in $user_skill_dir"
                fi
            elif [ -e "$dest" ]; then
                warn "Skipping $(basename "$source") in $user_skill_dir: path already exists and is not a symlink"
            else
                ln -s "$source" "$dest"
                ok "Linked $(basename "$source") into $user_skill_dir"
            fi
            linked_count=$((linked_count + 1))
        done
    done
    if [ "$linked_count" -eq 0 ]; then
        info "No bundled skills found under skills/"
    fi
fi

# 7. PATH wiring — make ~/.zshrc the source of truth, independent of the live
#    PATH (which may carry only a temporary entry that won't persist to new
#    shells). Always ensure a correct marked block pointing at THIS APP_DIR.
desired_block="$(
    printf '%s\n' "$PATH_MARKER_OPEN"
    printf 'export PATH="%s:$PATH"\n' "$APP_DIR"
    printf '%s\n' "$PATH_MARKER_CLOSE"
)"

# Strip ALL existing cowork blocks (handles stale/old-clone/duplicate ones),
# then append exactly one fresh block. Result is the same no matter the prior
# state — fully idempotent.
cleaned=""
if [ -f "$ZSHRC" ]; then
    cleaned="$(awk -v o="$PATH_MARKER_OPEN" -v c="$PATH_MARKER_CLOSE" '
        $0 == o {skip=1}
        skip && $0 == c {skip=0; next}
        !skip {print}
    ' "$ZSHRC")"
fi
desired_rc="$cleaned"$'\n\n'"$desired_block"

if [ -f "$ZSHRC" ] && [ "$(cat "$ZSHRC")" = "$desired_rc" ]; then
    ok "~/.zshrc already exports this cowork dir"
else
    if [ -f "$ZSHRC" ] && grep -qF "$PATH_MARKER_OPEN" "$ZSHRC"; then
        info "Replaced stale cowork PATH block(s) in ~/.zshrc"
    fi
    printf '%s\n' "$desired_rc" > "$ZSHRC"
    ok "Wrote PATH block to ~/.zshrc"
fi

# Does the *current* shell already resolve it? Only then is no reload needed.
path_action="reload"
case ":$PATH:" in
    *":$APP_DIR:"*) path_action="" ;;
esac

# 8. Verify deps + controller CLIs (verify-only, no auto-install).
echo
info "Running preflight…"
echo
check_status=0
"$VENV_PY" "$APP_DIR/cowork" --check || check_status=$?

# 9. Summary.
echo
if [ "$check_status" -eq 0 ]; then
    ok "cowork is ready."
else
    warn "Preflight reported missing pieces above (e.g. claude / codex). Install them, then re-run: cowork --check"
fi
if [ "$path_action" = "reload" ]; then
    echo
    info "Open a new terminal (or run: source ~/.zshrc), then from any folder:"
    info "  cowork            # .cowork/ session lands in the current directory"
fi
