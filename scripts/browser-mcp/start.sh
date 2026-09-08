#!/usr/bin/env bash
# Wrapper so Claude Code (which has a minimal PATH) can find node via nvm.
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
exec node "$(dirname "$0")/index.mjs"
