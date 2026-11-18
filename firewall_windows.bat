@echo off
REM Run this ONCE on the MONITOR laptop: right-click -> "Run as administrator".
REM Opens UDP 5000 (reports), 5001 (alerts + ping), 5002 (discovery) and TCP 8080 (dashboard).
netsh advfirewall firewall delete rule name="CN Health Monitor UDP" >nul 2>&1
netsh advfirewall firewall delete rule name="CN Health Monitor Web" >nul 2>&1
netsh advfirewall firewall add rule name="CN Health Monitor UDP" dir=in action=allow protocol=UDP localport=5000-5002 profile=any
netsh advfirewall firewall add rule name="CN Health Monitor Web" dir=in action=allow protocol=TCP localport=8080 profile=any
echo.
echo Done. Firewall rules added.
pause
