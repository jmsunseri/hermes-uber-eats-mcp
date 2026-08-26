#!/usr/bin/env bash
#
# install.sh — Uber Eats Search MCP Server installer
#
# Installs the MCP server and its dependencies into a Hermes Agent
# environment. Safe to re-run (idempotent).
#
# Usage:
#   ./install.sh                          # interactive (prompts for address)
#   ./install.sh --address "Taipei 101"   # non-interactive
#   ./install.sh --venv /path/to/venv     # custom venv (default: auto-detect)
#   ./install.sh --help
#
set -euo pipefail

# ── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
error() { echo -e "${RED}✗${NC} $1" >&2; }
title() { echo -e "\n${BOLD}=== $1 ===${NC}\n"; }

# ── Defaults ─────────────────────────────────────────────────────────────────
ADDRESS=""
CUSTOM_VENV=""
SCRIPTS_DIR="$HOME/.hermes/scripts"
CONFIG_FILE="uber_eats_config.json"

# ── Parse args ──────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --address) ADDRESS="$2"; shift 2 ;;
        --venv)    CUSTOM_VENV="$2"; shift 2 ;;
        --help|-h)
            echo "Usage: ./install.sh [--address 'Delivery address'] [--venv /path/to/venv]"
            echo ""
            echo "Options:"
            echo "  --address  Delivery address (Google Places query, e.g. 'Taipei 101')"
            echo "  --venv     Python venv with Hermes + camoufox (default: auto-detect)"
            echo "  --help     Show this help"
            exit 0
            ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Pre-flight checks ───────────────────────────────────────────────────────
title "Uber Eats Search MCP Server — Installer"

# Check we're on Linux or macOS
OS="$(uname -s)"
case "$OS" in
    Linux|Darwin) info "Platform: $OS" ;;
    *) error "Unsupported OS: $OS (Linux or macOS required)"; exit 1 ;;
esac

# Check Python 3.11+
title "Checking Python"
if command -v python3 &>/dev/null; then
    PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    info "System Python: $PY_VERSION"
    PY_OK=$(python3 -c 'import sys; exit(0 if sys.version_info >= (3,11) else 1)' && echo yes || echo no)
    if [[ "$PY_OK" != "yes" ]]; then
        warn "System Python is $PY_VERSION, need 3.11+. Will rely on venv."
    fi
else
    error "Python 3 not found. Install Python 3.11+ first."
    exit 1
fi

# ── Detect Hermes venv ──────────────────────────────────────────────────────
title "Detecting Hermes venv"

if [[ -n "$CUSTOM_VENV" ]]; then
    VENV_PYTHON="$CUSTOM_VENV/bin/python"
    if [[ ! -x "$VENV_PYTHON" ]]; then
        error "Venv Python not found at: $VENV_PYTHON"
        exit 1
    fi
    info "Using custom venv: $CUSTOM_VENV"
elif [[ -x "$HOME/.hermes/hermes-agent/venv/bin/python" ]]; then
    VENV_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python"
    info "Using Hermes venv: $(dirname "$(dirname "$VENV_PYTHON")")"
else
    # No Hermes venv — create a dedicated one
    warn "Hermes venv not found. Creating a dedicated venv."
    VENV_DIR="$HOME/.hermes/venvs/uber-eats-mcp"
    python3 -m venv "$VENV_DIR"
    VENV_PYTHON="$VENV_DIR/bin/python"
    info "Created venv: $VENV_DIR"
fi

# Verify venv Python version
VENV_VERSION=$("$VENV_PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Venv Python: $VENV_VERSION"

# ── Install Python dependencies ─────────────────────────────────────────────
title "Installing Python dependencies"

"$VENV_PYTHON" -m pip install --upgrade pip --quiet
info "pip upgraded"

# Install mcp (Model Context Protocol SDK)
if "$VENV_PYTHON" -c "import mcp" 2>/dev/null; then
    info "mcp already installed"
else
    info "Installing mcp..."
    "$VENV_PYTHON" -m pip install "mcp>=2.0.0" --quiet
    info "mcp installed"
fi

# Install camoufox (anti-detection Firefox browser)
if "$VENV_PYTHON" -c "import camoufox" 2>/dev/null; then
    info "camoufox already installed"
else
    info "Installing camoufox..."
    "$VENV_PYTHON" -m pip install "camoufox[geoip]" --quiet
    info "camoufox installed"
fi

# Install playwright (dependency of camoufox, but make sure)
if "$VENV_PYTHON" -c "import playwright" 2>/dev/null; then
    info "playwright already installed"
else
    info "Installing playwright..."
    "$VENV_PYTHON" -m pip install playwright --quiet
    info "playwright installed"
fi

# ── Download Camoufox browser binary ────────────────────────────────────────
title "Downloading Camoufox browser binary"

# Check if already downloaded
CAMOUFOX_CHECK=$("$VENV_PYTHON" -c "
from camoufox.pkgman import installed_verstr
try:
    print(installed_verstr())
except Exception:
    print('NOT_FOUND')
" 2>/dev/null || echo "NOT_FOUND")

if [[ "$CAMOUFOX_CHECK" != "NOT_FOUND" ]]; then
    info "Camoufox browser already downloaded (v$CAMOUFOX_CHECK)"
else
    info "Downloading Camoufox browser (one-time, ~100MB)..."
    "$VENV_PYTHON" -m camoufox fetch
    info "Camoufox browser downloaded"
fi

# ── Copy scripts ────────────────────────────────────────────────────────────
title "Installing scripts to $SCRIPTS_DIR"

mkdir -p "$SCRIPTS_DIR"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

for FILE in \
    uber_eats_mcp_server.py \
    uber_eats_search.py \
    uber_eats_format.py \
    uber_eats_config.json \
    README.md
do
    SRC="$SCRIPT_DIR/$FILE"
    DST="$SCRIPTS_DIR/$FILE"

    if [[ ! -f "$SRC" ]]; then
        warn "Skipping $FILE (not found in source)"
        continue
    fi

    # Skip if source and dest are the same file
    if [[ "$SRC" -ef "$DST" ]]; then
        info "$FILE already in place"
        continue
    fi

    cp "$SRC" "$DST"
    info "Installed: $FILE"
done

# ── Create or update config ─────────────────────────────────────────────────
title "Configuring delivery address"

CONFIG_PATH="$SCRIPTS_DIR/$CONFIG_FILE"

# Prompt for address if not provided
if [[ -z "$ADDRESS" ]]; then
    EXISTING_ADDR=""
    if [[ -f "$CONFIG_PATH" ]]; then
        EXISTING_ADDR=$(python3 -c "
import json
with open('$CONFIG_PATH') as f:
    print(json.load(f).get('default_address', ''))
" 2>/dev/null || echo "")
    fi

    if [[ -n "$EXISTING_ADDR" ]]; then
        read -rp "Delivery address [default: $EXISTING_ADDR]: " ADDRESS
        ADDRESS="${ADDRESS:-$EXISTING_ADDR}"
    else
        read -rp "Delivery address (Google Places query, e.g. 'Taipei 101'): " ADDRESS
        if [[ -z "$ADDRESS" ]]; then
            warn "No address provided. Using default 'Mandarin Oriental Taipei'"
            ADDRESS="Mandarin Oriental Taipei"
        fi
    fi
fi

# Write config (merge with existing if present)
"$VENV_PYTHON" -c "
import json, os

config_path = '$CONFIG_PATH'
config = {
    'default_address': '$ADDRESS',
    'max_stores': 30,
    'output_dir': '/tmp',
    'locale': 'tw-en',
    'city_url': 'https://www.ubereats.com/tw-en/city/taipei-tpe'
}

if os.path.exists(config_path):
    with open(config_path) as f:
        existing = json.load(f)
    existing.update(config)
    config = existing

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write('\n')

print(f'Default address: {config[\"default_address\"]}')
"

info "Config written to $CONFIG_PATH"

# ── Register MCP server in Hermes config ─────────────────────────────────────
title "Registering MCP server in Hermes config"

HERMES_CONFIG="$HOME/.hermes/config.yaml"

if [[ ! -f "$HERMES_CONFIG" ]]; then
    error "Hermes config not found at $HERMES_CONFIG"
    warn "Create it with: hermes setup"
    warn "Then re-run this installer."
    exit 1
fi

# Check if already registered
ALREADY_REGISTERED=$(python3 -c "
import yaml
with open('$HERMES_CONFIG') as f:
    cfg = yaml.safe_load(f)
servers = cfg.get('mcp_servers', {})
print('yes' if 'uber-eats-search' in servers else 'no')
" 2>/dev/null || echo "no")

if [[ "$ALREADY_REGISTERED" == "yes" ]]; then
    info "MCP server already registered in Hermes config"
else
    # Register using hermes config set
    hermes config set mcp_servers.uber-eats-search.command "$VENV_PYTHON" 2>/dev/null || true
    hermes config set mcp_servers.uber-eats-search.args "[\"$SCRIPTS_DIR/uber_eats_mcp_server.py\"]" 2>/dev/null || true
    hermes config set mcp_servers.uber-eats-search.enabled true 2>/dev/null || true

    # Verify it was set
    VERIFY=$(python3 -c "
import yaml
with open('$HERMES_CONFIG') as f:
    cfg = yaml.safe_load(f)
servers = cfg.get('mcp_servers', {})
print('yes' if 'uber-eats-search' in servers else 'no')
" 2>/dev/null || echo "no")

    if [[ "$VERIFY" == "yes" ]]; then
        info "MCP server registered in Hermes config"
    else
        warn "Could not auto-register via 'hermes config set'. Add manually:"
        echo ""
        echo "  mcp_servers:"
        echo "    uber-eats-search:"
        echo "      command: $VENV_PYTHON"
        echo "      args:"
        echo "        - $SCRIPTS_DIR/uber_eats_mcp_server.py"
        echo "      enabled: true"
        echo ""
    fi
fi

# ── Verify ───────────────────────────────────────────────────────────────────
title "Verifying installation"

# Verify MCP server can start
VERIFY_OUTPUT=$(echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2026-07-28","capabilities":{},"clientInfo":{"name":"installer","version":"1.0"}}}' | timeout 5 "$VENV_PYTHON" "$SCRIPTS_DIR/uber_eats_mcp_server.py" 2>/dev/null || echo "FAIL")

if echo "$VERIFY_OUTPUT" | grep -q '"uber-eats-search"'; then
    info "MCP server starts and responds correctly"
else
    warn "MCP server verification failed. Check dependencies:"
    warn "  $VENV_PYTHON -c 'import mcp, camoufox'"
fi

# ── Summary ──────────────────────────────────────────────────────────────────
title "Installation Complete"

echo "  Venv:       $VENV_PYTHON"
echo "  Scripts:    $SCRIPTS_DIR/"
echo "  Config:     $CONFIG_PATH"
echo "  MCP server: uber-eats-search (in Hermes config)"
echo ""
echo -e "${BOLD}Tools available to your agent:${NC}"
echo "  uber_eats_search  — Search restaurants, collect menu items (5-10 min)"
echo "  uber_eats_format   — Filter and format results into Markdown"
echo ""
echo -e "${BOLD}To test:${NC}"
echo "  Ask your agent: 'find me kung pao chicken on uber eats'"
echo ""