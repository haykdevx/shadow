#compdef shadow shadow-backup shadow-calendar shadow-contacts shadow-cookbook shadow-docs shadow-gallery shadow-mail shadow-mcp shadow-memory shadow-notes shadow-personal shadow-preset shadow-research shadow-sessions shadow-signature shadow-skills shadow-tasks shadow-theme shadow-webhook
# Zsh tab-completion for the shadow umbrella + sub-CLIs.
#
# Drop in any directory on $fpath, e.g.:
#     fpath=(/path/to/shadow-ui/scripts/_completion $fpath)
#     autoload -U compinit; compinit
#
# Then `shadow <tab>` completes subcommands; `shadow mail <tab>`
# completes mail subcommands; `shadow-mail <tab>` works the same.

_shadow_scripts_dir() {
    local self="${(%):-%x}"
    while [[ -L "$self" ]]; do self="$(readlink "$self")"; done
    cd "${self:h}/.." && pwd
}

typeset -gA _shadow_subs

_shadow_refresh() {
    _shadow_subs=()
    local dir="$(_shadow_scripts_dir)"
    local py="$dir/../venv/bin/python"
    [[ -x "$py" ]] || py="$(command -v python3)"
    local f sub help_out commands
    for f in "$dir"/shadow-*; do
        [[ -x "$f" ]] || continue
        case "$f" in
            *.bak|*.pyc|*.pre-*) continue ;;
        esac
        sub="${${f:t}#shadow-}"
        help_out=$("$py" "$f" --help 2>/dev/null) || continue
        commands=$(echo "$help_out" | grep -oE '\{[a-z0-9_,-]+\}' | head -1 \
            | tr -d '{}' | tr ',' ' ')
        _shadow_subs[$sub]="$commands"
    done
}

_shadow() {
    [[ ${#_shadow_subs} -eq 0 ]] && _shadow_refresh

    local cmd="${words[1]}"

    if [[ "$cmd" == "shadow" ]]; then
        if (( CURRENT == 2 )); then
            local -a subs=(${(k)_shadow_subs} help)
            _describe 'subcommand' subs
            return
        fi
        local sub="${words[2]}"
        if [[ "$sub" == "help" ]] && (( CURRENT == 3 )); then
            local -a subs=(${(k)_shadow_subs})
            _describe 'subcommand' subs
            return
        fi
        if (( CURRENT == 3 )); then
            local -a sc=(${(s/ /)_shadow_subs[$sub]})
            _describe 'command' sc
            return
        fi
        return
    fi

    # shadow-foo <tab>
    local sub="${cmd#shadow-}"
    if (( CURRENT == 2 )); then
        local -a sc=(${(s/ /)_shadow_subs[$sub]})
        _describe 'command' sc
        return
    fi
}

_shadow "$@"
