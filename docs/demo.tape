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
Set TypingSpeed 60ms

Type "redis-cli -p 6380"  Enter  Sleep 700ms

# --- strings + sorted sets ---
Type "SET greeting 'hello from miniredis'"      Enter  Sleep 700ms
Type "ZADD leaderboard 100 alice 250 bob 175 carol"  Enter  Sleep 700ms
Type "ZRANGE leaderboard 0 -1"                  Enter  Sleep 1200ms

# --- transactions: queue, then run atomically ---
Type "MULTI"                Enter  Sleep 600ms
Type "INCR visits"          Enter  Sleep 500ms
Type "INCR visits"          Enter  Sleep 500ms
Type "ZADD leaderboard 300 dave"  Enter  Sleep 500ms
Type "EXEC"                 Enter  Sleep 1400ms

# --- TTL: set a 2s expiry, watch it count down and vanish ---
Type "SET session:42 active EX 2"   Enter  Sleep 600ms
Type "TTL session:42"               Enter  Sleep 900ms
Type "GET session:42"               Enter  Sleep 2500ms
Type "# waited for the TTL to pass..."  Enter  Sleep 400ms
Type "TTL session:42"               Enter  Sleep 700ms
Type "GET session:42"               Enter  Sleep 1500ms

Sleep 1500ms
