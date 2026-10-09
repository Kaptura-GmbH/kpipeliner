#!/usr/bin/env bash
# kpipeliner installer / uninstaller
#
# Usage:
#   ./install.sh                       – install, using the default python
#                                         environment (whatever `python3`
#                                         resolves to right now) to run kpl
#                                         itself and any node with no env of
#                                         its own
#   ./install.sh --venv                – install, creating/using a venv at
#                                         the default location (<root>/.venv)
#   ./install.sh --venv /path/to/env   – install, creating (if missing) or
#                                         reusing an existing venv at that path
#   ./install.sh --uninstall           – uninstall

set -e

PIPELINER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$PIPELINER_DIR/kpl"
LEGACY_LAUNCHER="$PIPELINER_DIR/kpipeliner"   # old command name
CONFIG_YAML="$PIPELINER_DIR/config.yaml"

# ─── Uninstall ────────────────────────────────────────────────────────────────
if [[ "$1" == "--uninstall" ]]; then
  echo "Uninstalling kpl (kpipeliner)..."

  # Remove PATH entry from shell configs
  for RC in "$HOME/.bashrc" "$HOME/.zshrc" "$HOME/.profile"; do
    if [[ -f "$RC" ]]; then
      # Remove the export line we added
      ESCAPED=$(echo "$PIPELINER_DIR" | sed 's/[\/&]/\\&/g')
      sed -i "/export PATH=\$PATH:$ESCAPED/d" "$RC" 2>/dev/null || true
      echo "  Cleaned PATH from $RC"
    fi
  done

  for L in "$LAUNCHER" "$LEGACY_LAUNCHER"; do
    if [[ -f "$L" ]]; then
      rm -f "$L"
      echo "  Removed launcher: $L"
    fi
  done

  echo "kpl (kpipeliner) uninstalled."
  echo "Note: no python environment or source files were removed (nothing was deleted"
  echo "      that this script didn't create itself) — config.yaml's python_env still"
  echo "      points wherever it did. Delete $PIPELINER_DIR manually if desired."
  exit 0
fi

# ─── Parse install options ─────────────────────────────────────────────────────
USE_VENV=0
VENV_DIR=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --venv)
      USE_VENV=1
      if [[ -n "$2" && "$2" != --* ]]; then
        VENV_DIR="$2"
        shift 2
      else
        VENV_DIR="$PIPELINER_DIR/.venv"
        shift
      fi
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Usage: $0 [--venv [path]] | --uninstall" >&2
      exit 1
      ;;
  esac
done

# ─── Install ──────────────────────────────────────────────────────────────────
echo "Installing kpl (kpipeliner) from: $PIPELINER_DIR"

# 1. Resolve the python environment: a venv (created if missing, reused if it
#    already exists) when --venv was given, otherwise whatever `python3`/`pip3`
#    currently resolve to on PATH (the "default python environment").
if [[ "$USE_VENV" == "1" ]]; then
  if [[ ! -d "$VENV_DIR" ]]; then
    echo "  Creating Python virtual environment at $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
  else
    echo "  Using existing virtual environment at $VENV_DIR."
  fi
  PYTHON="$VENV_DIR/bin/python"
  PIP="$VENV_DIR/bin/pip"
else
  echo "  No --venv given: using the default python environment."
  PYTHON="$(command -v python3 || command -v python || true)"
  PIP="$(command -v pip3 || command -v pip || true)"
  if [[ -z "$PYTHON" ]]; then
    echo "  ERROR: no python3/python found on PATH." >&2
    exit 1
  fi
  if [[ -z "$PIP" ]]; then
    echo "  ERROR: no pip3/pip found on PATH." >&2
    exit 1
  fi
fi

echo "  Installing Python dependencies..."
"$PIP" install --upgrade pip --quiet
"$PIP" install -r "$PIPELINER_DIR/requirements.txt" --quiet

# 2. Persist the choice in config.yaml so kpl (and node runs with no env of
#    their own) resolve to the same python every time, not just right after
#    install. Absent/null python_env means "default python environment".
set_python_env_config() {
  local val="$1"   # absolute venv path, or empty for "none"
  [[ -f "$CONFIG_YAML" ]] || touch "$CONFIG_YAML"
  if grep -q '^python_env:' "$CONFIG_YAML"; then
    if [[ -z "$val" ]]; then
      sed -i 's|^python_env:.*|python_env: null|' "$CONFIG_YAML"
    else
      sed -i "s|^python_env:.*|python_env: $val|" "$CONFIG_YAML"
    fi
  else
    {
      echo ""
      echo "# Python environment kpl itself runs in, and the fallback for any node"
      echo "# that declares no environment of its own. Absolute path to a venv dir,"
      echo "# or null to use the default python environment. Set by install.sh"
      echo "# (--venv [path], or no flag for null) — edit by hand + reinstall to change."
      if [[ -z "$val" ]]; then
        echo "python_env: null"
      else
        echo "python_env: $val"
      fi
    } >> "$CONFIG_YAML"
  fi
}
set_python_env_config "$VENV_DIR"
echo "  config.yaml: python_env = ${VENV_DIR:-null}"

# 3. Build GUI frontend if npm is available
FRONTEND_DIR="$PIPELINER_DIR/kpipeline_gui/frontend"
DIST_DIR="$FRONTEND_DIR/dist"

if command -v npm &>/dev/null; then
  if [[ ! -d "$DIST_DIR" ]]; then
    echo "  Building GUI frontend..."
    cd "$FRONTEND_DIR"
    npm install --silent
    npm run build --silent
    cd "$PIPELINER_DIR"
    echo "  GUI frontend built."
  else
    echo "  GUI frontend already built (run 'npm run build' in kpipeline_gui/frontend to rebuild)."
  fi
else
  echo "  WARNING: npm not found. GUI frontend will not be available."
  echo "           Install Node.js then re-run install.sh or run: cd kpipeline_gui/frontend && npm install && npm run build"
fi

# 4. Create launcher script
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
"$PYTHON" "$PIPELINER_DIR/kpipeline.py" "\$@"
EOF
chmod +x "$LAUNCHER"
echo "  Created launcher: $LAUNCHER"

# 5. Add to PATH in shell rc files
add_to_path() {
  local RC="$1"
  local ENTRY="export PATH=\$PATH:$PIPELINER_DIR"
  if [[ -f "$RC" ]] && grep -qF "$PIPELINER_DIR" "$RC"; then
    echo "  PATH already contains $PIPELINER_DIR in $RC"
  elif [[ -f "$RC" ]]; then
    echo "" >> "$RC"
    echo "# kpipeliner" >> "$RC"
    echo "$ENTRY" >> "$RC"
    echo "  Added to PATH in $RC"
  fi
}

add_to_path "$HOME/.bashrc"
[[ -f "$HOME/.zshrc" ]] && add_to_path "$HOME/.zshrc"

echo ""
echo "✓ kpl (kpipeliner) installed successfully!"
echo ""
echo "  Restart your terminal or run:"
echo "    source ~/.bashrc"
echo ""
echo "  Then use:"
echo "    kpl init          – initialize a pipeline in current dir"
echo "    kpl list          – list available node types"
echo "    kpl add <type>    – add a node"
echo "    kpl run           – run the pipeline"
echo "    kpl gui           – launch the web GUI"
echo "    kpl --help        – show all commands"
echo ""
echo "  To uninstall: $PIPELINER_DIR/install.sh --uninstall"
echo ""
echo "  Optional, for connecting an LLM client (kpl mcp / kpl gui --mcp):"
echo "    $PIP install -r $PIPELINER_DIR/requirements-mcp.txt"
