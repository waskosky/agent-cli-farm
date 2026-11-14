#!/usr/bin/env bash

# Shared, read-only diagnostic. Sourced only when tmux is missing; use builtins
# so this still works with an incomplete PATH. Never run a package manager here.
printf '%s\n' \
  'tmux not found on PATH; farm sessions require tmux.' \
  'setup.sh installs farm helpers; the optional tmux-deep-history plugin does not install tmux.' \
  'Install tmux in the terminal environment where you run the farm (inside WSL on Windows):'

if command -v apt-get >/dev/null 2>&1; then
  printf '%s\n' '  sudo apt-get install tmux'
elif command -v apt >/dev/null 2>&1; then
  printf '%s\n' '  sudo apt install tmux'
elif command -v brew >/dev/null 2>&1; then
  printf '%s\n' '  brew install tmux'
elif command -v dnf >/dev/null 2>&1; then
  printf '%s\n' '  sudo dnf install tmux'
elif command -v yum >/dev/null 2>&1; then
  printf '%s\n' '  sudo yum install tmux'
elif command -v pacman >/dev/null 2>&1; then
  printf '%s\n' '  sudo pacman -S tmux'
elif command -v zypper >/dev/null 2>&1; then
  printf '%s\n' '  sudo zypper install tmux'
elif command -v apk >/dev/null 2>&1; then
  printf '%s\n' '  sudo apk add tmux'
elif command -v pkg >/dev/null 2>&1; then
  printf '%s\n' '  sudo pkg install tmux'
else
  printf '%s\n' \
    '  Ubuntu/Debian/WSL Ubuntu: sudo apt-get install tmux' \
    '  macOS with Homebrew: brew install tmux' \
    '  Other systems: install tmux using your system package manager.'
fi

printf '%s\n' \
  'If tmux is already installed, add its directory to PATH in this shell.' \
  'Verify with: tmux -V, then retry your farm command.'
