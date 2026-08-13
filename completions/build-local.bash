#!/usr/bin/env bash
# Bash completion for build_local.sh
# To enable: source completions/build-local.bash

_build_local_completions() {
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local prev="${COMP_WORDS[COMP_CWORD-1]}"

    local script_path="${COMP_WORDS[0]}"
    local script_dir
    script_dir="$(cd "$(dirname "$script_path")" 2>/dev/null && pwd)"

    local commands="init update list build clean clean_all gitignore copy help"

    # Resolve targets from build.yaml
    _build_targets() {
        local build_config="${script_dir}/build.yaml"
        [ -f "$build_config" ] || return
        grep -E '^\s+artifact-name:' "$build_config" | sed 's/.*artifact-name:[[:space:]]*//' | tr -d '"'
    }

    # Find the subcommand already on the line (first non-flag word after the script)
    local subcommand=""
    for ((i = 1; i < COMP_CWORD; i++)); do
        local word="${COMP_WORDS[i]}"
        if [[ "$word" != -* ]]; then
            subcommand="$word"
            break
        fi
    done

    case "$subcommand" in
        build)
            local targets
            targets=$(_build_targets)
            COMPREPLY=($(compgen -W "$targets" -- "$cur"))
            ;;
        clean)
            # Complete with build artifact dirs that exist, plus yaml targets
            local targets
            targets=$(_build_targets)
            local build_dir="${script_dir}/build"
            if [ -d "$build_dir" ]; then
                local built
                built=$(ls -1 "$build_dir" 2>/dev/null | tr '\n' ' ')
                targets="$targets $built"
            fi
            COMPREPLY=($(compgen -W "$targets" -- "$cur"))
            ;;
        copy)
            COMPREPLY=($(compgen -d -- "$cur"))
            ;;
        "")
            # No subcommand yet — complete flags or commands
            case "$cur" in
                -*)
                    COMPREPLY=($(compgen -W "-i --incremental" -- "$cur"))
                    ;;
                *)
                    COMPREPLY=($(compgen -W "$commands" -- "$cur"))
                    ;;
            esac
            ;;
        *)
            # Unknown subcommand or already complete
            COMPREPLY=()
            ;;
    esac
}

complete -F _build_local_completions ./build_local.sh
complete -F _build_local_completions build_local.sh
