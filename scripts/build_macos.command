#!/usr/bin/env sh
set -eu
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_command=python3
if command -v python3.12 >/dev/null 2>&1; then
    python_command=python3.12
fi
if "$python_command" "$script_directory/build.py" "$@"; then
    exit 0
fi
printf '\nBuild failed. Press Enter to close.\n'
read -r response
exit 1
