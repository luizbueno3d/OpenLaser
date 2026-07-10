-- OpenLaser launcher.
-- 1. If the UI server is already running on 127.0.0.1:8765, just open the browser.
-- 2. Otherwise, start it inside a Terminal window so the user can see logs and
--    close the window to quit. Then open the browser ~2 seconds later.

set appPath to POSIX path of (path to me)
-- The app is expected to live in the project root (see README). Resolve the
-- folder that contains the .app bundle.
set projectPath to do shell script ("dirname " & quoted form of appPath)
set venvPython to projectPath & "/.venv/bin/python"
try
    do shell script ("test -x " & quoted form of venvPython)
on error
    display dialog "OpenLaser.app must stay in the OpenLaser project folder (next to .venv). Could not find " & venvPython buttons {"OK"} default button "OK" with icon stop
    return
end try
set portNumber to 8765
set serverURL to "http://127.0.0.1:" & portNumber & "/"
set safetyURL to serverURL & "api/safety"

set quotedProject to quoted form of projectPath
set startCommand to "cd " & quotedProject & " && ./.venv/bin/python -m mopa_luiz ui --port " & portNumber

-- Probe whether the server is already up.
set alreadyUp to false
try
    set probeResult to do shell script "curl -s -o /dev/null -w '%{http_code}' --max-time 1 " & quoted form of safetyURL
    if probeResult is "200" then set alreadyUp to true
end try

if alreadyUp then
    do shell script "open " & quoted form of serverURL
    return
end if

-- Free any stale process bound to our port.
try
    do shell script "lsof -ti :" & portNumber & " | xargs -r kill -9"
end try

-- Start the server inside Terminal so the user can see it and close it cleanly.
tell application "Terminal"
    activate
    do script startCommand
end tell

-- Give the server a moment to bind, then open the browser.
delay 2
do shell script "open " & quoted form of serverURL
