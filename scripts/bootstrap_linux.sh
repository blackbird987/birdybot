#!/usr/bin/env bash
# One-shot first-boot setup for this bot on a fresh Linux install.
#
# Replaces the manual sequence in docs/linux-migration.md §6: venv, install,
# path migration, worktree cleanup, systemd service, smoke test. Idempotent —
# safe to re-run if a step fails and you fix it.
#
#   chmod +x scripts/bootstrap_linux.sh
#   ./scripts/bootstrap_linux.sh
#
# It will NOT: install Python/Node/the Claude CLI (distro's job), or log you
# in (interactive OAuth). It stops and tells you when it needs those.
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

# --- 1. prerequisites -------------------------------------------------------
step "Checking prerequisites"

command -v python3 >/dev/null || die "python3 not found — install it first."
PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
    || die "Python $PYV is too old — this project needs 3.11+."
ok "python3 $PYV"

command -v git >/dev/null || die "git not found — install it first."
ok "git $(git --version | awk '{print $3}')"

if command -v claude >/dev/null; then
    ok "claude CLI at $(command -v claude)"
else
    warn "claude CLI NOT on PATH."
    warn "Install it, then re-run this script — otherwise the migration step"
    warn "will guess the binary path and every spawn will fail."
    read -r -p "  Continue anyway? [y/N] " reply
    [[ "$reply" == [yY] ]] || exit 1
fi

[[ -f .env ]] || die ".env is missing — copy it across from the Windows install first."
ok ".env present"

# --- 2. virtualenv ----------------------------------------------------------
step "Creating virtualenv"
if [[ -d .venv ]]; then
    ok ".venv already exists — reusing"
else
    python3 -m venv .venv || die "venv creation failed"
    ok ".venv created"
fi
./.venv/bin/pip install --quiet --upgrade pip || warn "pip self-upgrade failed (continuing)"
./.venv/bin/pip install --quiet -e . || die "dependency install failed"
ok "dependencies installed"

# --- 3. rewrite Windows paths ----------------------------------------------
step "Migrating paths (dry run first)"
./.venv/bin/python scripts/migrate_to_linux.py || die "migration preview failed"

echo
read -r -p "Apply these changes? [y/N] " reply
if [[ "$reply" == [yY] ]]; then
    ./.venv/bin/python scripts/migrate_to_linux.py --apply || die "migration failed"
    ok "paths rewritten (originals kept as *.windows.bak)"
else
    warn "skipped — the bot will not find its repos until this is applied"
fi

# --- 4. stale worktrees -----------------------------------------------------
step "Cleaning stale build worktrees"
git worktree prune
if [[ -d .worktrees ]]; then
    find .worktrees -maxdepth 1 -mindepth 1 -type d -exec rm -rf {} + 2>/dev/null || true
fi
ok "worktrees pruned — now: $(git worktree list | wc -l) checkout(s)"

# --- 5. systemd user service ------------------------------------------------
step "Installing systemd user service"
if ! command -v systemctl >/dev/null; then
    warn "systemd not present — skipping. Start the bot with ./start.sh instead."
else
    mkdir -p ~/.config/systemd/user
    # %h in the unit resolves to $HOME, but only if the repo really lives at
    # the path the unit assumes. Rewrite it to wherever we actually are.
    sed "s|%h/Desktop/Programming/claude-telegram-bot|$REPO|g" \
        scripts/claude-bot.service > ~/.config/systemd/user/claude-bot.service
    systemctl --user daemon-reload
    ok "unit installed (WorkingDirectory=$REPO)"

    if loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
        ok "lingering already enabled"
    else
        warn "Lingering is OFF — the bot would stop when you log out of the desktop."
        echo "  Enabling it needs one sudo password:"
        if sudo loginctl enable-linger "$USER"; then
            ok "lingering enabled"
        else
            warn "could not enable lingering — run this yourself later:"
            warn "    sudo loginctl enable-linger $USER"
        fi
    fi
fi

# --- 6. accounts ------------------------------------------------------------
step "Checking Claude accounts"
ACCOUNTS=$(grep -E '^CLAUDE_ACCOUNTS=' .env | cut -d= -f2- | tr ',' ' ')
if [[ -z "$ACCOUNTS" ]]; then
    warn "CLAUDE_ACCOUNTS not set in .env"
else
    for acct in $ACCOUNTS; do
        acct="${acct/#\~/$HOME}"
        if [[ -f "$acct/.credentials.json" ]]; then
            ok "$(basename "$acct") — credentials present"
        else
            warn "$(basename "$acct") — NOT signed in. Run:"
            warn "    CLAUDE_CONFIG_DIR=$acct claude     then /login"
        fi
    done
fi

# --- 7. smoke test ----------------------------------------------------------
step "Smoke test"
if [[ -f scripts/smoke_test.py ]]; then
    ./.venv/bin/python scripts/smoke_test.py || warn "smoke test reported problems (see above)"
else
    warn "scripts/smoke_test.py not found — skipped"
fi

# --- done -------------------------------------------------------------------
step "Next"
cat <<EOF
  Sign in to any account flagged above, then start the bot:

      systemctl --user enable --now claude-bot
      journalctl --user -u claude-bot -f

  Or without systemd:   ./start.sh

  Then from Discord:    /repo list     (all 13 should resolve)
EOF
