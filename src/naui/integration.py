"""Generate temporary Bash/zsh rc contents; never modify user startup files."""
import base64
import shlex
import sys


def local_functions():
    python = shlex.quote(sys.executable)
    return f'''
unalias nano ssh sudo 2>/dev/null || true
nano() {{ command {python} -m naui.shell open "$@"; }}
ssh() {{ command {python} -m naui.shell ssh "$@"; }}
sudo() {{ command {python} -m naui.shell sudo "$@"; }}
'''


def local_bash_rc():
    return '''# Generated for this Naui terminal only.
if [ -r "$HOME/.bashrc" ]; then source "$HOME/.bashrc"; fi
''' + local_functions() + local_bash_hooks()


def local_bash_hooks():
    python = shlex.quote(sys.executable)
    # Append to PROMPT_COMMAND, preserving scalar and array hooks from .bashrc.
    # PS0 runs before a completed command; prompts/cwd are never scraped.
    return f'''
__naui_prompt() {{
    local naui_status=$?
    if [[ ${{__naui_run_history-0}} == 1 ]]; then
        set -o history
        history -d "${{__naui_run_history_start}}" 2>/dev/null || true
    fi
    unset __naui_run_history
    unset __naui_run_history_start
    command {python} -m naui.shell cwd
    return "$naui_status"
}}
PROMPT_COMMAND+=(__naui_prompt)
PS0='$(command {python} -m naui.shell busy)'"${{PS0-}}"
PS2='$(command {python} -m naui.shell busy)'"${{PS2-}}"
__naui_prepare_navigation() {{
    if ! shopt -q promptvars || [[ $PS2 != *'naui.shell busy'* ]]; then
        command {python} -m naui.shell unsupported 'Navigation requires Bash promptvars and the Naui continuation hook. Restart Naui to restore session integration.'
        return
    fi
    READLINE_LINE=''
    READLINE_POINT=0
    command {python} -m naui.shell navigation-ready
}}
bind -x '"\\e[777;1~":__naui_prepare_navigation'
__naui_prepare_run() {{
    __naui_run_history_start=$HISTCMD
    __naui_run_history=0
    [[ -o history ]] && __naui_run_history=1
    set +o history
    __naui_prepare_navigation
}}
bind -x '"\\e[777;2~":__naui_prepare_run'
export -f nano ssh sudo __naui_prompt
'''


def local_zsh_rc():
    return '''# Generated for this Naui terminal only.
if [ -n "$NAUI_ORIGINAL_ZDOTDIR" ] && [ -r "$NAUI_ORIGINAL_ZDOTDIR/.zshrc" ]; then
    source "$NAUI_ORIGINAL_ZDOTDIR/.zshrc"
fi
''' + local_functions() + f'''
__naui_prompt() {{
    if [[ ${{__naui_run_history-0}} == 1 ]]; then fc -P; unset __naui_run_history; fi
    command {shlex.quote(sys.executable)} -m naui.shell cwd
}}
__naui_preexec() {{ command {shlex.quote(sys.executable)} -m naui.shell busy; }}
precmd_functions+=(__naui_prompt)
preexec_functions+=(__naui_preexec)
__naui_prepare_navigation() {{
    [[ $CONTEXT == start ]] || return
    BUFFER=''
    CURSOR=0
    command {shlex.quote(sys.executable)} -m naui.shell navigation-ready
}}
zle -N __naui_prepare_navigation
bindkey '^[[777;1~' __naui_prepare_navigation
__naui_prepare_run() {{
    [[ $CONTEXT == start ]] || return
    fc -p /dev/null 0 0
    __naui_run_history=1
    __naui_prepare_navigation
}}
zle -N __naui_prepare_run
bindkey '^[[777;2~' __naui_prepare_run
'''


# Bash emits JSON without any remote Python package/helper. UTF-8 bytes are
# preserved; JSON controls, quotes and backslashes are escaped. Base64 contains
# no terminal delimiters. `base64`, `cat`, `stat`, etc. are existing coreutils.
REMOTE_FUNCTIONS = r'''
unalias nano sudo 2>/dev/null || true
__naui_json() {
    local LC_ALL=C s=$1 c n i
    printf '"'
    for ((i=0; i<${#s}; i++)); do
        c=${s:i:1}
        case "$c" in
            '"') printf '\\"' ;;
            '\') printf '\\\\' ;;
            *) printf -v n '%d' "'$c"
               if ((n < 32)); then printf '\\u%04x' "$n"; else printf '%s' "$c"; fi ;;
        esac
    done
    printf '"'
}
nano() {
    local arg sep='' payload options=1 cwd
    local -a files=()
    for arg in "$@"; do
        if ((options)) && [[ "$arg" == -- ]]; then options=0
        elif ((options)) && [[ "$arg" == [-+]* ]]; then
            printf '%s\n' 'Naui: nano flags are not supported. Use /usr/bin/nano.' >&2
            return 2
        else files+=("$arg"); fi
    done
    if ((${#files[@]} == 0 || ${#files[@]} > 32)); then
        printf '%s\n' 'Naui: usage: nano FILE [FILE ...], at most 32 files.' >&2
        return 2
    fi
    cwd=$(pwd -P && printf .)
    cwd=${cwd%.}
    cwd=${cwd%$'\n'}
    payload=$({
        printf '{"v":1,"action":"open","location":"remote","token":"%s","session":"%s","cwd":' "$__NAUI_TOKEN" "$__NAUI_SESSION"
        __naui_json "$cwd"
        printf ',"args":['
        for arg in "${files[@]}"; do printf '%s' "$sep"; __naui_json "$arg"; sep=,; done
        printf '],"elevated":false}'
    } | base64 -w0)
    if ((${#payload} > 16384)); then
        printf '%s\n' 'Naui: request exceeds the protocol size limit.' >&2
        return 2
    fi
    printf '\033]777;naui;%s\007' "$payload"
}
sudo() {
    # Keep elevated commands in the real terminal; sudo owns authentication.
    command sudo "$@"
}
export -f nano sudo __naui_json
printf '\033]777;naui;%s\007' "$(printf '{"v":1,"action":"session-ready","location":"remote","token":"%s","session":"%s"}' "$__NAUI_TOKEN" "$__NAUI_SESSION" | base64 -w0)"
__naui_remote_prompt() {
    local naui_status=$? cwd payload action=${1:-cwd}
    if [[ $action == cwd ]]; then
        if [[ ${__naui_run_history-0} == 1 ]]; then
            set -o history
            history -d "${__naui_run_history_start}" 2>/dev/null || true
        fi
        unset __naui_run_history
        unset __naui_run_history_start
    fi
    cwd=$(pwd -P && printf .)
    cwd=${cwd%.}
    cwd=${cwd%$'\n'}
    payload=$({
        printf '{"v":1,"action":"%s","location":"remote","token":"%s","session":"%s","path":' "$action" "$__NAUI_TOKEN" "$__NAUI_SESSION"
        __naui_json "$cwd"
        printf ',"home":'
        __naui_json "$HOME"
        printf '}'
    } | base64 -w0)
    if ((${#payload} <= 16384)); then printf '\033]777;naui;%s\007' "$payload"; fi
    return "$naui_status"
}
__naui_remote_busy() {
    printf '\033]777;naui;%s\007' "$(printf '{"v":1,"action":"busy","location":"remote","token":"%s","session":"%s"}' "$__NAUI_TOKEN" "$__NAUI_SESSION" | base64 -w0)"
}
PROMPT_COMMAND+=(__naui_remote_prompt)
PS0='$(__naui_remote_busy)'"${PS0-}"
PS2='$(__naui_remote_busy)'"${PS2-}"
__naui_prepare_navigation() {
    if ! shopt -q promptvars || [[ $PS2 != *'__naui_remote_busy'* ]]; then
        printf '%s\n' 'Naui: Navigation requires Bash promptvars and the Naui continuation hook. Reconnect to restore session integration.' >&2
        return
    fi
    READLINE_LINE=''
    READLINE_POINT=0
    __naui_remote_prompt navigation-ready
}
bind -x '"\e[777;1~":__naui_prepare_navigation'
__naui_prepare_run() {
    __naui_run_history_start=$HISTCMD
    __naui_run_history=0
    [[ -o history ]] && __naui_run_history=1
    set +o history
    __naui_prepare_navigation
}
bind -x '"\e[777;2~":__naui_prepare_run'
export -f __naui_remote_prompt __naui_remote_busy
'''


def remote_command(token, session):
    rc = ('if [ -r "$HOME/.bashrc" ]; then source "$HOME/.bashrc"; fi\n'
          + "__NAUI_TOKEN=" + shlex.quote(token) + "\n__NAUI_SESSION=" + shlex.quote(session)
          + "\nexport __NAUI_TOKEN __NAUI_SESSION\n" + REMOTE_FUNCTIONS)
    encoded = base64.b64encode(rc.encode()).decode("ascii")
    # The outer login shell need not understand process substitution. Bash does.
    inner = f"exec bash --rcfile <(printf %s {shlex.quote(encoded)} | base64 -d) -i"
    return "bash -c " + shlex.quote(inner)
