#!/usr/bin/env bash
#
# Installs the agy / opencode delegation MCP servers and registers them with
# Claude Code. Safe to re-run: it replaces whatever it installed last time.
#
#   ./install.sh                          # both servers, user scope
#   ./install.sh --servers opencode       # just one
#   ./install.sh --default-cwd ~/code/app # pin a project instead of session cwd
#   ./install.sh --check                  # is the installed copy current?
#   ./install.sh --uninstall              # remove registrations and files
#
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_DIR="${AGENT_MCP_HOME:-$HOME/.local/share/agent-delegation-mcp}"
PY_VERSION="3.13"
SERVERS="agy,opencode"
SCOPE="user"
DEFAULT_CWD=""
REGISTER=1
UNINSTALL=0
CHECK=0
ASSUME_YES=0

if [ -t 1 ]; then
  B=$'\033[1m'; R=$'\033[31m'; Y=$'\033[33m'; G=$'\033[32m'; N=$'\033[0m'
else
  B=""; R=""; Y=""; G=""; N=""
fi
info() { printf '%s\n' "$*"; }
step() { printf '%s==>%s %s\n' "$B" "$N" "$*"; }
warn() { printf '%swarning:%s %s\n' "$Y" "$N" "$*" >&2; }
ok()   { printf '%s  ok%s %s\n' "$G" "$N" "$*"; }
die()  { printf '%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Installs the agy / opencode delegation MCP servers and registers them with
Claude Code. Safe to re-run.

  ./install.sh                          both servers, user scope
  ./install.sh --servers opencode       just one
  ./install.sh --default-cwd ~/code/app pin a project instead of session cwd
  ./install.sh --check                  report installed vs repo version
  ./install.sh --uninstall              remove registrations and files

Options:
  --dir PATH           install location (default ~/.local/share/agent-delegation-mcp)
  --servers LIST       comma-separated: agy, opencode (default both)
  --python VERSION     interpreter version for the venv (default 3.13)
  --default-cwd PATH   pin AGENT_MCP_DEFAULT_CWD; omit to use each session's cwd
  --scope SCOPE        claude mcp scope: user, project or local (default user)
  --no-register        install files only, skip `claude mcp add`
  --check              compare the installed copy against this checkout and exit
                       (exit 1 if stale, so it is usable as a guard in CI)
  --uninstall          deregister the servers and delete the installed files
  -y, --yes            do not prompt on uninstall
  -h, --help           this text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)          INSTALL_DIR="${2:?--dir needs a path}"; shift 2 ;;
    --servers)      SERVERS="${2:?--servers needs a list}"; shift 2 ;;
    --python)       PY_VERSION="${2:?--python needs a version}"; shift 2 ;;
    --default-cwd)  DEFAULT_CWD="${2:?--default-cwd needs a path}"; shift 2 ;;
    --scope)        SCOPE="${2:?--scope needs a value}"; shift 2 ;;
    --no-register)  REGISTER=0; shift ;;
    --check)        CHECK=1; shift ;;
    --uninstall)    UNINSTALL=1; shift ;;
    -y|--yes)       ASSUME_YES=1; shift ;;
    -h|--help)      usage; exit 0 ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
done

WANT_AGY=0; WANT_OPENCODE=0
IFS=',' read -r -a _servers <<< "$SERVERS"
for s in "${_servers[@]}"; do
  case "${s// /}" in
    agy)      WANT_AGY=1 ;;
    opencode) WANT_OPENCODE=1 ;;
    "")       ;;
    *)        die "unknown server: $s (expected agy and/or opencode)" ;;
  esac
done
[ "$WANT_AGY" = 1 ] || [ "$WANT_OPENCODE" = 1 ] || die "--servers selected nothing"

VENV="$INSTALL_DIR/.venv"

deregister() {
  if command -v claude >/dev/null 2>&1; then
    if claude mcp remove "$1" -s "$SCOPE" >/dev/null 2>&1; then
      ok "deregistered $1"
    else
      warn "$1 was not registered in $SCOPE scope"
    fi
  else
    warn "\`claude\` is not on PATH; skipping deregistration of $1"
  fi
}

# ------------------------------------------------------------------ version --
# The servers are installed as plain file copies, so a stale install is
# invisible: the only evidence is an mtime compared against `git log` by hand.
# That is exactly how a committed process-group fix sat uninstalled through a
# real incident. Stamp what was installed, and give both this script and the
# servers' delegation_status tool something to compare against.
src_commit() { git -C "$SRC_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown; }

write_version() {
  local commit describe dirty
  commit="$(src_commit)"
  if [ "$commit" != unknown ]; then
    describe="$(git -C "$SRC_DIR" describe --tags --always --dirty 2>/dev/null || echo "$commit")"
    # -uno: untracked files cannot change what was just copied, and counting
    # them would stamp every install "dirty" for an unrelated scratch file.
    if [ -n "$(git -C "$SRC_DIR" status --porcelain -uno 2>/dev/null)" ]; then dirty=1; else dirty=0; fi
  else
    describe="unknown (not a git checkout)"; dirty=0
  fi
  cat > "$INSTALL_DIR/VERSION" <<EOF
commit=$commit
describe=$describe
dirty=$dirty
installed=$(date -u +%Y-%m-%dT%H:%M:%SZ)
source=$SRC_DIR
EOF
}

if [ "$CHECK" = 1 ]; then
  [ -f "$INSTALL_DIR/VERSION" ] || die "no VERSION at $INSTALL_DIR: either nothing is
         installed there, or the install predates version stamping. Either way it is
         not the current code - run ./install.sh"
  i_commit=""; i_when=""; i_src=""; i_dirty=0
  while IFS='=' read -r k v; do
    case "$k" in
      commit)    i_commit="$v" ;;
      installed) i_when="$v" ;;
      source)    i_src="$v" ;;
      dirty)     i_dirty="$v" ;;
    esac
  done < "$INSTALL_DIR/VERSION"
  head="$(src_commit)"
  info "installed: $i_commit ($i_when, from ${i_src:-unknown})"
  info "repo HEAD: $head ($SRC_DIR)"
  if [ "$i_commit" = unknown ] || [ "$head" = unknown ]; then
    warn "cannot compare: one side is not a git checkout"
    exit 0
  fi
  if [ "$i_commit" != "$head" ]; then
    die "STALE: installed $i_commit, this checkout is at $head. Run ./install.sh
         (and then /mcp reconnect - a running server holds the old code in memory)."
  fi
  [ "$i_dirty" = 1 ] && warn "installed from a dirty working tree; the files may not match $head"
  ok "up to date at $head"
  exit 0
fi

# ---------------------------------------------------------------- uninstall --
# Deletes only what this script installs, and only after confirming the venv is
# a venv. It never recursively removes an arbitrary --dir.
if [ "$UNINSTALL" = 1 ]; then
  [ "$WANT_AGY" = 1 ]      && deregister agy-wrapper
  [ "$WANT_OPENCODE" = 1 ] && deregister opencode-wrapper

  if [ -d "$INSTALL_DIR" ]; then
    if [ "$ASSUME_YES" != 1 ]; then
      printf 'Delete the installed files under %s ? [y/N] ' "$INSTALL_DIR"
      read -r reply </dev/tty || reply=""
      case "$reply" in
        [yY]*) ;;
        *) info "Left $INSTALL_DIR in place."; exit 0 ;;
      esac
    fi
    if [ -f "$VENV/pyvenv.cfg" ]; then
      find "$VENV" -depth -delete
      ok "removed the venv"
    elif [ -e "$VENV" ]; then
      warn "$VENV does not look like a venv (no pyvenv.cfg); left alone"
    fi
    for f in agy_mcp_server.py opencode_mcp_server.py requirements.txt README.md VERSION; do
      [ -f "$INSTALL_DIR/$f" ] && rm -f "$INSTALL_DIR/$f"
    done
    [ -d "$INSTALL_DIR/__pycache__" ] && find "$INSTALL_DIR/__pycache__" -depth -delete
    rmdir "$INSTALL_DIR" 2>/dev/null && ok "removed $INSTALL_DIR" \
      || info "Left $INSTALL_DIR in place: it still contains files this script did not install."
  fi
  info "Uninstalled. Restart Claude Code (or /mcp reconnect) to drop the tools."
  exit 0
fi

# ------------------------------------------------------------- delegate CLIs --
# Resolved to absolute paths, because an MCP server is spawned by Claude Code
# and does not reliably inherit a login shell's PATH (nvm CLIs in particular).
AGY_BIN=""; OPENCODE_BIN=""
if [ "$WANT_AGY" = 1 ]; then
  AGY_BIN="$(command -v agy || true)"
  if [ -n "$AGY_BIN" ]; then
    ok "agy: $AGY_BIN ($(agy --version 2>/dev/null | head -1 || echo 'version unknown'))"
  else
    warn "\`agy\` was not found on PATH. Installing anyway: the tool resolves it at
         run time, or set AGY_BIN and re-run. Antigravity also needs one
         interactive login before headless use works."
  fi
fi
if [ "$WANT_OPENCODE" = 1 ]; then
  OPENCODE_BIN="$(command -v opencode || true)"
  if [ -n "$OPENCODE_BIN" ]; then
    ok "opencode: $OPENCODE_BIN ($(opencode --version 2>/dev/null | head -1 || echo 'version unknown'))"
    case "$OPENCODE_BIN" in
      */.nvm/versions/node/*)
        warn "that path carries the node version and will break on a node upgrade.
         Re-run this script afterwards, or set OPENCODE_BIN to a stable symlink
         such as /usr/local/bin/opencode." ;;
    esac
  else
    warn "\`opencode\` was not found on PATH. Installing anyway: the tool resolves
         it at run time, or set OPENCODE_BIN and re-run."
  fi
fi

# -------------------------------------------------------------------- files --
step "Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
[ "$WANT_AGY" = 1 ]      && install -m 0644 "$SRC_DIR/agy_mcp_server.py" "$INSTALL_DIR/"
[ "$WANT_OPENCODE" = 1 ] && install -m 0644 "$SRC_DIR/opencode_mcp_server.py" "$INSTALL_DIR/"
install -m 0644 "$SRC_DIR/requirements.txt" "$INSTALL_DIR/"
[ -f "$SRC_DIR/README.md" ] && install -m 0644 "$SRC_DIR/README.md" "$INSTALL_DIR/"
write_version
ok "server files copied ($(sed -n 's/^describe=//p' "$INSTALL_DIR/VERSION"))"

# --------------------------------------------------------------------- venv --
# Pin the interpreter. A stock `python3 -m venv` leaves bin/python as a symlink
# to whatever python3 becomes later; when a brew or distro upgrade moves it, the
# venv's site-packages no longer match and every MCP server here fails to start
# with no error anywhere. The tools just vanish from Claude's tool list.
step "Building the venv (python $PY_VERSION)"
# --clear on both paths: uv refuses an existing venv outright, and a reused venv
# would keep whatever interpreter it was built with, defeating the pin above.
if command -v uv >/dev/null 2>&1; then
  uv venv --clear --python "$PY_VERSION" "$VENV" >/dev/null
  uv pip install --python "$VENV/bin/python" -r "$INSTALL_DIR/requirements.txt" >/dev/null
  ok "uv-managed CPython $PY_VERSION, mcp installed"
else
  warn "uv was not found, falling back to the system python3. If a later upgrade
         moves that interpreter the tools will silently disappear. Installing uv
         (https://docs.astral.sh/uv/) and re-running avoids that."
  command -v python3 >/dev/null 2>&1 || die "neither uv nor python3 is available"
  python3 -m venv --clear "$VENV"
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
  ok "venv built with $("$VENV/bin/python" -V)"
fi

# --------------------------------------------------------------- smoke test --
# run_name is deliberately not __main__, so each module is imported and its tool
# registered without entering the stdio loop.
step "Smoke-testing the servers"
smoke() {
  "$VENV/bin/python" - "$1" <<'PY' || die "$(basename "$1") failed to load; see the traceback above"
import runpy, sys
runpy.run_path(sys.argv[1], run_name="smoke")
PY
  ok "$(basename "$1") loads"
}
[ "$WANT_AGY" = 1 ]      && smoke "$INSTALL_DIR/agy_mcp_server.py"
[ "$WANT_OPENCODE" = 1 ] && smoke "$INSTALL_DIR/opencode_mcp_server.py"

# ---------------------------------------------------------------- register --
if [ "$REGISTER" = 0 ]; then
  info ""
  info "Files installed, registration skipped. Register manually with:"
  [ "$WANT_AGY" = 1 ]      && info "  claude mcp add agy-wrapper -s $SCOPE -- $VENV/bin/python $INSTALL_DIR/agy_mcp_server.py"
  [ "$WANT_OPENCODE" = 1 ] && info "  claude mcp add opencode-wrapper -s $SCOPE -- $VENV/bin/python $INSTALL_DIR/opencode_mcp_server.py"
  exit 0
fi

command -v claude >/dev/null 2>&1 \
  || die "\`claude\` is not on PATH. Re-run with --no-register, or install Claude Code first."

register() {
  local name="$1" script="$2"; shift 2
  claude mcp remove "$name" -s "$SCOPE" >/dev/null 2>&1 || true
  if ! claude mcp add "$name" -s "$SCOPE" "$@" -- "$VENV/bin/python" "$script" >/dev/null; then
    die "\`claude mcp add $name\` failed. The files are installed and smoke-tested at
         $INSTALL_DIR, but $name is currently deregistered. Re-run this script to retry."
  fi
  ok "registered $name ($SCOPE scope)"
}

step "Registering with Claude Code"
if [ "$WANT_AGY" = 1 ]; then
  args=()
  [ -n "$AGY_BIN" ]     && args+=(-e "AGY_BIN=$AGY_BIN")
  [ -n "$DEFAULT_CWD" ] && args+=(-e "AGENT_MCP_DEFAULT_CWD=$DEFAULT_CWD")
  register agy-wrapper "$INSTALL_DIR/agy_mcp_server.py" ${args+"${args[@]}"}
fi
if [ "$WANT_OPENCODE" = 1 ]; then
  args=()
  [ -n "$OPENCODE_BIN" ] && args+=(-e "OPENCODE_BIN=$OPENCODE_BIN")
  [ -n "$DEFAULT_CWD" ]  && args+=(-e "AGENT_MCP_DEFAULT_CWD=$DEFAULT_CWD")
  register opencode-wrapper "$INSTALL_DIR/opencode_mcp_server.py" ${args+"${args[@]}"}
fi

# A narrowed --servers run installs only the named servers but leaves any
# previously-installed one in place and still registered. Surface that rather
# than silently orphaning it; don't delete it, since the user may want to keep it.
if [ "$WANT_AGY" = 0 ] && [ -f "$INSTALL_DIR/agy_mcp_server.py" ]; then
  warn "agy was not selected, but it is still installed and may still be registered.
         To remove it:  claude mcp remove agy-wrapper -s $SCOPE && rm $INSTALL_DIR/agy_mcp_server.py"
fi
if [ "$WANT_OPENCODE" = 0 ] && [ -f "$INSTALL_DIR/opencode_mcp_server.py" ]; then
  warn "opencode was not selected, but it is still installed and may still be registered.
         To remove it:  claude mcp remove opencode-wrapper -s $SCOPE && rm $INSTALL_DIR/opencode_mcp_server.py"
fi

# Resolved with an if, not ${VAR:-default}: an apostrophe in the default word
# opens a quote inside the expansion and bash never finds the closing brace.
if [ -n "$DEFAULT_CWD" ]; then
  SCOPE_DESC="$DEFAULT_CWD"
else
  SCOPE_DESC="the working directory of each Claude session"
fi

cat <<EOF

${B}Done.${N} Restart Claude Code, or run /mcp reconnect in an open session.

  Tools:   mcp__agy-wrapper__ask_agy, mcp__opencode-wrapper__ask_opencode
  Check:   claude mcp list, ./install.sh --check
  Version: $(sed -n 's/^describe=//p' "$INSTALL_DIR/VERSION")
  Scope:   ${SCOPE_DESC}

The delegates these tools launch approve their own shell commands, file edits
and git operations under that directory, with no checkpoint mid-run. Read the
"Operating rules" section of the README before the first real dispatch.
EOF
