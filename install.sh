#!/usr/bin/env bash
# SoloCUPS installer: per-user, no sudo. Installs to ~/.local and never touches system files.
set -e
cd "$(dirname "$0")"

missing=()
python3 -c "import PyQt6.QtWidgets, PyQt6.QtSvg" 2>/dev/null || missing+=("PyQt6")
command -v lpstat >/dev/null 2>&1 || missing+=("CUPS")

if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing: ${missing[*]}"
  echo "Install them first, then run this again:"
  echo "  Arch/CachyOS:    sudo pacman -S python-pyqt6 cups polkit"
  echo "  Debian/Ubuntu:   sudo apt install python3-pyqt6 python3-pyqt6.qtsvg cups policykit-1"
  echo "  Fedora:          sudo dnf install python3-pyqt6 cups polkit"
  echo "Then make sure CUPS is running:  sudo systemctl enable --now cups"
  exit 1
fi

bin="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$bin" "$apps" "$icons"
install -m755 solocups.py "$bin/solocups"
install -m644 icons/solocups.svg "$icons/solocups.svg"
install -m644 packaging/solocups.desktop "$apps/solocups.desktop"
update-desktop-database "$apps" 2>/dev/null || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "Installed. Launch SoloCUPS from your app menu, or run: solocups"
case ":$PATH:" in *":$bin:"*) ;; *) echo "Note: add $bin to your PATH to run it from a terminal." ;; esac
if ! systemctl is-active --quiet cups 2>/dev/null; then
  echo "CUPS isn't running. Start it with:  sudo systemctl enable --now cups"
fi
