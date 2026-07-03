# miniredis demo recording — render with:  vhs docs/demo.tape
# (install: brew install vhs)  ->  writes docs/demo.gif
#
# Assumes a miniredis server is already running on port 6380:
#   uv run python -m miniredis

Output docs/demo.gif

Set Shell bash
Set FontSize 18
Set Width 1100
Set Height 720
Set Padding 20
Set TypingSpeed 50ms

# Force a clean, minimal shell prompt and clear the screen BEFORE recording
# begins. Without this, the shell's own (zsh-style) prompt config leaks in as
# `git_prompt_info: command not found` and literal `%{[green]%}` color codes.
# Everything between Hide and Show runs but is not shown in the GIF.
Hide
Type "unset PROMPT_COMMAND" Enter
Type "export PS1='$ '"      Enter
Type "clear"                Enter
Show

Type "redis-cli -p 6380"  Enter  Sleep 800ms

# --- strings + sorted sets ---
Type "SET greeting 'hello from miniredis'"      Enter  Sleep 700ms
Type "ZADD leaderboard 100 alice 250 bob 175 carol"  Enter  Sleep 700ms
Type "ZRANGE leaderboard 0 -1"                  Enter  Sleep 1200ms

# --- transactions: queue commands, then run them atomically ---
Type "MULTI"                      Enter  Sleep 500ms
Type "INCR visits"                Enter  Sleep 400ms
Type "INCR visits"                Enter  Sleep 400ms
Type "ZADD leaderboard 300 dave"  Enter  Sleep 400ms
Type "EXEC"                       Enter  Sleep 1400ms

# --- TTL: 6s expiry so it survives the typing animation; show it live,
#     then a silent pause (redis-cli has no comment syntax), then it's gone.
Type "SET session:42 active EX 6"  Enter  Sleep 400ms
Type "TTL session:42"              Enter  Sleep 700ms
Type "GET session:42"             Enter  Sleep 800ms
Sleep 5s
Type "GET session:42"             Enter  Sleep 700ms
Type "TTL session:42"             Enter  Sleep 1500ms

Sleep 1200ms
