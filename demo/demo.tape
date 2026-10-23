# vhs tape for the README GIF. All data is the deterministic fake seed.
# Render from the repository root:  vhs demo/demo.tape
# The tape starts the mock servers itself (hrsync serve in the background) and stops them at the end.
Output demo/demo.gif
Set FontSize 15
Set Width 1000
Set Height 600
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms
Set Padding 16

Hide
Type "uv run hrsync serve > /dev/null 2>&1 &"
Enter
Sleep 3s
Type "clear"
Enter
Show

Type "uv run hrsync seed"
Sleep 400ms
Enter
Sleep 2s

Type "uv run hrsync run"
Sleep 400ms
Enter
Sleep 4s

Type "uv run hrsync queue"
Sleep 400ms
Enter
Sleep 3s

Type `uv run hrsync resolve 1 --skip --note "location not in rules yet"`
Sleep 400ms
Enter
Sleep 2500ms

Type "clear && uv run hrsync report | head -30"
Sleep 400ms
Enter
Sleep 6s

Hide
Type "kill %1"
Enter
Sleep 500ms
