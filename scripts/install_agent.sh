#!/bin/bash
# Install handoff_agent.py as a macOS Launch Agent (auto-start at login).
#
# Usage:
#   bash scripts/install_agent.sh --chat-id <ID> --project-dir <DIR> [--model <MODEL>]
#   bash scripts/install_agent.sh --uninstall
#   bash scripts/install_agent.sh --status
#
# The script creates a plist in ~/Library/LaunchAgents/ and loads it.
# The agent starts immediately and restarts if it exits.

set -euo pipefail

LABEL="com.handoff.agent"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="/tmp/handoff"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"
AGENT_SCRIPT="${SCRIPT_DIR}/handoff_agent.py"

# Parse arguments
ACTION="install"
CHAT_ID=""
PROJECT_DIR=""
MODEL="claude-opus-4-6"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --chat-id) CHAT_ID="$2"; shift 2 ;;
        --project-dir) PROJECT_DIR="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --uninstall) ACTION="uninstall"; shift ;;
        --status) ACTION="status"; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

uninstall() {
    if launchctl list "$LABEL" &>/dev/null; then
        echo "Stopping $LABEL..."
        launchctl unload "$PLIST" 2>/dev/null || true
    fi
    if [[ -f "$PLIST" ]]; then
        rm "$PLIST"
        echo "Removed $PLIST"
    else
        echo "Not installed."
    fi
}

status() {
    if launchctl list "$LABEL" &>/dev/null; then
        echo "Status: RUNNING"
        launchctl list "$LABEL"
    else
        echo "Status: NOT RUNNING"
    fi
    if [[ -f "$PLIST" ]]; then
        echo "Plist: $PLIST"
    fi
    echo ""
    echo "Logs:"
    tail -5 "$LOG_DIR/handoff-agent.log" 2>/dev/null || echo "  (no log file)"
}

install() {
    if [[ -z "$CHAT_ID" ]]; then
        echo "Error: --chat-id is required"
        echo "Usage: bash scripts/install_agent.sh --chat-id <ID> --project-dir <DIR>"
        exit 1
    fi
    if [[ -z "$PROJECT_DIR" ]]; then
        PROJECT_DIR="$HOME"
    fi
    PROJECT_DIR="$(cd "$PROJECT_DIR" && pwd)"

    # Check prerequisites
    if [[ ! -f "$AGENT_SCRIPT" ]]; then
        echo "Error: $AGENT_SCRIPT not found"
        exit 1
    fi
    if ! "$PYTHON" -c "import claude_agent_sdk" 2>/dev/null; then
        echo "Error: claude-agent-sdk not installed. Run: pip3 install claude-agent-sdk"
        exit 1
    fi

    # Check auth: OAuth (claude auth login) or API key
    API_KEY="${ANTHROPIC_API_KEY:-}"
    if [[ -z "$API_KEY" ]]; then
        # Check if OAuth is available (claude auth status)
        if command -v claude &>/dev/null && claude auth status &>/dev/null 2>&1; then
            echo "Using OAuth authentication (from 'claude auth login')."
        else
            echo "No authentication found."
            echo "Either run 'claude auth login' first, or set ANTHROPIC_API_KEY."
            read -rp "Enter API key (or press Enter to use OAuth): " API_KEY
        fi
    fi

    # Stop if already running
    if launchctl list "$LABEL" &>/dev/null; then
        echo "Stopping existing agent..."
        launchctl unload "$PLIST" 2>/dev/null || true
    fi

    # Ensure log directory
    mkdir -p "$LOG_DIR"

    # Build environment dict
    ENV_DICT=""
    if [[ -n "$API_KEY" ]]; then
        # Resolve SSL cert file for Python under launchd (certifi or system)
    SSL_CERT=$("$PYTHON" -c "try:
    import certifi; print(certifi.where())
except ImportError:
    import ssl; print(ssl.get_default_verify_paths().cafile or '')" 2>/dev/null)

    # Capture proxy settings from current shell (crucial for GFW regions)
    PROXY_HTTP="${http_proxy:-${HTTP_PROXY:-}}"
    PROXY_HTTPS="${https_proxy:-${HTTPS_PROXY:-}}"
    PROXY_ALL="${all_proxy:-${ALL_PROXY:-}}"

    ENV_DICT="    <key>EnvironmentVariables</key>
    <dict>
        <key>ANTHROPIC_API_KEY</key>
        <string>${API_KEY}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$HOME/.local/bin:$HOME/.nvm/versions/node/$(node -v 2>/dev/null || echo v18.0.0)/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>${SSL_CERT:+
        <key>SSL_CERT_FILE</key>
        <string>${SSL_CERT}</string>}${PROXY_HTTP:+
        <key>http_proxy</key>
        <string>${PROXY_HTTP}</string>}${PROXY_HTTPS:+
        <key>https_proxy</key>
        <string>${PROXY_HTTPS}</string>}${PROXY_ALL:+
        <key>all_proxy</key>
        <string>${PROXY_ALL}</string>}
    </dict>"
    else
        # Resolve SSL cert file for Python under launchd (certifi or system)
        SSL_CERT=$("$PYTHON" -c "try:
    import certifi; print(certifi.where())
except ImportError:
    import ssl; print(ssl.get_default_verify_paths().cafile or '')" 2>/dev/null)

        # Capture proxy settings from current shell (crucial for GFW regions)
        PROXY_HTTP="${http_proxy:-${HTTP_PROXY:-}}"
        PROXY_HTTPS="${https_proxy:-${HTTPS_PROXY:-}}"
        PROXY_ALL="${all_proxy:-${ALL_PROXY:-}}"

        ENV_DICT="    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:/opt/homebrew/sbin:$HOME/.local/bin:$HOME/.nvm/versions/node/$(node -v 2>/dev/null || echo v18.0.0)/bin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>LANG</key>
        <string>en_US.UTF-8</string>${SSL_CERT:+
        <key>SSL_CERT_FILE</key>
        <string>${SSL_CERT}</string>}${PROXY_HTTP:+
        <key>http_proxy</key>
        <string>${PROXY_HTTP}</string>}${PROXY_HTTPS:+
        <key>https_proxy</key>
        <string>${PROXY_HTTPS}</string>}${PROXY_ALL:+
        <key>all_proxy</key>
        <string>${PROXY_ALL}</string>}
    </dict>"
    fi

    # Write plist
    cat > "$PLIST" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${AGENT_SCRIPT}</string>
        <string>--chat-id</string>
        <string>${CHAT_ID}</string>
        <string>--project-dir</string>
        <string>${PROJECT_DIR}</string>
        <string>--model</string>
        <string>${MODEL}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/handoff-agent.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/handoff-agent.err</string>
${ENV_DICT}
</dict>
</plist>
PLIST_EOF

    echo "Created $PLIST"

    # Load and start
    launchctl load "$PLIST"
    echo ""
    echo "✓ Handoff agent installed and started!"
    echo ""
    echo "  Chat ID:     $CHAT_ID"
    echo "  Project dir: $PROJECT_DIR"
    echo "  Model:       $MODEL"
    echo "  Log:         $LOG_DIR/handoff-agent.log"
    echo ""
    echo "Commands:"
    echo "  Status:    bash $0 --status"
    echo "  Stop:      launchctl unload $PLIST"
    echo "  Start:     launchctl load $PLIST"
    echo "  Uninstall: bash $0 --uninstall"
    echo "  Logs:      tail -f $LOG_DIR/handoff-agent.log"
}

case "$ACTION" in
    install) install ;;
    uninstall) uninstall ;;
    status) status ;;
esac
