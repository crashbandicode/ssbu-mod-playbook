param(
    [Parameter(Mandatory)][string]$Label,
    [Parameter(Mandatory)][string]$Tag,
    [ValidateSet('a', 'b', 'none')][string]$PressS = 'a',
    [ValidateSet('a', 'b', 'none')][string]$PressY = 'a',
    [switch]$Search,
    [int]$MatchSeconds = 124,
    [int]$AfterSeconds = 36
)
# One match: optional search start, keepalive, then wait for the post-match
# popup in both events.logs, screenshot it, answer it, and screenshot after.
$e = $PSScriptRoot
$work = '/home/intpa/elite-rematch-work'
$rel = 'user/sdmc/ultimate/elite-rematch/events.log'
function Shot([string]$name) { foreach ($i in 's', 'y') { /usr/bin/python3 -I "$e/drive.py" $Label $i shot "$Tag-$name" | Out-Null } }
function Lines([string]$inst) {
    if ($inst -eq 'y') { @(Get-Content "$work/profiles/y/$rel") }
    else { @(/usr/bin/ssh -o BatchMode=yes intpa@dreamland-surface.lan "Get-Content $work/profiles/s/$rel") }
}
function Stamp { (Get-Date).ToUniversalTime().ToString('HH:mm:ss') + 'Z' }
$before = @{ s = (Lines 's').Count; y = (Lines 'y').Count }
if ($Search) {
    foreach ($i in 's', 'y') { /usr/bin/python3 -I "$e/drive.py" $Label $i press a | Out-Null }
    "$(Stamp) search started"
    Start-Sleep -Seconds 22
}
& "$e/match.ps1" -Label $Label -Seconds $MatchSeconds -ShotSeconds 0 -Tag $Tag | Out-Null
"$(Stamp) keepalive done"
$deadline = (Get-Date).AddSeconds(70)
$seen = @{ s = $false; y = $false }
while ((Get-Date) -lt $deadline -and -not ($seen.s -and $seen.y)) {
    foreach ($i in 's', 'y') {
        if (-not $seen[$i] -and ((Lines $i) | Select-Object -Skip $before[$i] | Where-Object { $_ -match ' popup record=' })) { $seen[$i] = $true; "$(Stamp) popup on $i" }
    }
    Start-Sleep -Milliseconds 700
}
Start-Sleep -Milliseconds 1200
Shot 'popup'
foreach ($p in @(@('s', $PressS), @('y', $PressY))) {
    if ($p[1] -ne 'none') { /usr/bin/python3 -I "$e/drive.py" $Label $p[0] press $p[1] | Out-Null }
}
"$(Stamp) answered s=$PressS y=$PressY"
$end = (Get-Date).AddSeconds($AfterSeconds)
$n = 0
while ((Get-Date) -lt $end) { $n++; Shot ('after{0:d2}' -f $n); Start-Sleep -Seconds 3 }
foreach ($i in 's', 'y') { "== $i"; Lines $i | Select-Object -Skip $before[$i] }
