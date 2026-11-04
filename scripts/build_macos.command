#!/usr/bin/env sh
set -eu
script_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python_command=/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12
if [ ! -x "$python_command" ]; then
    python_command=python3
fi
if "$python_command" "$script_directory/build.py" "$@"; then
    exit 0
fi
printf '\nBuild failed. Press Enter to close.\n'
read -r response
exit 1
