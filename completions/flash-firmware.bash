#!/usr/bin/env bash
# Bash completion for flash-firmware.sh
# To enable: source completions/flash-firmware.bash

_flash_firmware_completions() {
    local cur="${COMP_WORDS[COMP_CWORD]}"

    # Resolve the directory containing flash-firmware.sh
    local script_path="${COMP_WORDS[0]}"
    local script_dir
    script_dir="$(cd "$(dirname "$script_path")" 2>/dev/null && pwd)"

    local build_dir="${script_dir}/build"
    local targets=""

    if [ -d "$build_dir" ]; then
        targets=$(ls -1 "$build_dir" 2>/dev/null | tr '\n' ' ')
    fi

    COMPREPLY=($(compgen -W "$targets" -- "$cur"))
}

complete -F _flash_firmware_completions ./flash-firmware.sh
complete -F _flash_firmware_completions flash-firmware.sh
