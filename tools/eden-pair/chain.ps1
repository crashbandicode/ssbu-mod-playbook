param(
    [Parameter(Mandatory)][string]$Label,
    [Parameter(Mandatory)][string]$Tag,
    # One entry per expected popup: "<s button>/<y button>", e.g. a/a then b/b.
    [Parameter(Mandatory)][string[]]$Answers,
    [switch]$Search,
    [int]$MatchSeconds = 128,
    # Record a GIF on the Surface instance from the first popup.
    [string]$GifName,
    # Instances whose elite-rematch events.log signals the popup.
    [string[]]$Watch = @('s', 'y'),
    # Per match, the instance that runs off the stage once at the start
    # ('s', 'y' or 'none'), so the other wins the timed match by one KO.
    [string[]]$Losers = @(),
    # Seconds from the keepalive start to the walk-off (the match countdown).
    [int]$WalkDelay = 10,
    # Wait for the match itself: a new online session in the Yoga's desync
    # detector log (Elite vs non-Elite pairs only meet after the game's
    # search delay), keeping both players active in the lobby meanwhile.
    [switch]$DetectStart,
    # Natural Elite/non-Elite pairing: this instance searches first and waits
    # SearchGap seconds in the lobby (the game's Elite search widens after its
    # 120 s delay, parameter 1199) before the other one searches.
    [ValidateSet('s', 'y')][string]$FirstSearch = 's',
    [int]$SearchGap = 0,
    # With -Watch @() (shipped builds since 0.3.1 log only their install
    # line): seconds after the match to wait for the popup instead.
    [int]$PopupWait = 20
)
# A chain of matches: keepalive from the start of every match (including a
# rematch, which starts ~10 s after both say Yes), then answer each popup.
$e = $PSScriptRoot
$work = '/home/intpa/elite-rematch-work'
$rel = 'user/sdmc/ultimate/elite-rematch/events.log'
function Stamp { (Get-Date).ToUniversalTime().ToString('HH:mm:ss') + 'Z' }
function Lines([string]$inst) {
    if ($inst -eq 'y' -or (Test-Path "$work/runs/$Label-s")) { @(Get-Content "$work/profiles/$inst/$rel" -ErrorAction SilentlyContinue) }
    else { @(/usr/bin/ssh -o BatchMode=yes intpa@dreamland-surface.lan "Get-Content $work/profiles/s/$rel") }
}
function Shot([string]$name) { foreach ($i in 's', 'y') { /usr/bin/python3 -I "$e/drive.py" $Label $i shot "$Tag-$name" | Out-Null } }
$start = @{ s = (Lines 's').Count; y = (Lines 'y').Count }
if ($Search -and $SearchGap -gt 0) {
    $second = if ($FirstSearch -eq 's') { 'y' } else { 's' }
    /usr/bin/python3 -I "$e/drive.py" $Label $FirstSearch press a | Out-Null
    "$(Stamp) $FirstSearch searching alone for $SearchGap s"
    $stop = "$work/lobby-$Label-$Tag-first.stop"
    $alone = Start-Process -FilePath /usr/bin/python3 -PassThru -RedirectStandardOutput "$work/lobby-$Label-$Tag-first.out" -RedirectStandardError "$work/lobby-$Label-$Tag-first.err" -ArgumentList '-I', "$e/drive.py", $Label, $FirstSearch, 'keepalive', $stop, "$($SearchGap + 30)"
    Start-Sleep -Seconds $SearchGap
    New-Item -ItemType File -Force $stop | Out-Null
    $alone | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
    Shot 'alone'
    /usr/bin/python3 -I "$e/drive.py" $Label $second press a | Out-Null
    "$(Stamp) $second search started"
}
elseif ($Search) {
    foreach ($i in 's', 'y') { /usr/bin/python3 -I "$e/drive.py" $Label $i press a | Out-Null }
    "$(Stamp) search started"
    if (-not $DetectStart) { Start-Sleep -Seconds 22 }
}
for ($k = 0; $k -lt $Answers.Count; $k++) {
    $before = @{ s = (Lines 's').Count; y = (Lines 'y').Count }
    $loser = if ($k -lt $Losers.Count) { $Losers[$k] } else { 'none' }
    $seconds = $MatchSeconds
    $walkDelay = $WalkDelay
    if ($DetectStart) {
        $detector = "$work/profiles/y/user/sdmc/ultimate/ssbu-desync-detector/events.log"
        $sessions = @(Get-Content $detector -ErrorAction SilentlyContinue | Where-Object { $_ -match ' session slot=' }).Count
        $stop = "$work/lobby-$Label-$Tag-$k.stop"
        $lobby = foreach ($i in 's', 'y') {
            Start-Process -FilePath /usr/bin/python3 -PassThru -RedirectStandardOutput "$work/lobby-$Label-$Tag-$k-$i.out" -RedirectStandardError "$work/lobby-$Label-$Tag-$k-$i.err" -ArgumentList '-I', "$e/drive.py", $Label, $i, 'keepalive', $stop, '300'
        }
        $waitStart = Get-Date
        $deadline = $waitStart.AddSeconds(300)
        while ((Get-Date) -lt $deadline -and @(Get-Content $detector -ErrorAction SilentlyContinue | Where-Object { $_ -match ' session slot=' }).Count -le $sessions) {
            Start-Sleep -Seconds 1
        }
        New-Item -ItemType File -Force $stop | Out-Null
        $lobby | Wait-Process -Timeout 10 -ErrorAction SilentlyContinue
        "$(Stamp) match $k session started after $([int]((Get-Date) - $waitStart).TotalSeconds) s"
        $walkDelay = 5
    }
    if ($loser -ne 'none') {
        Start-Sleep -Seconds $walkDelay
        Shot "start$k"
        # Run right for 4 s: off the edge once; released before the respawn
        # platform appears, so only one self-destruct.
        /usr/bin/python3 -I "$e/drive.py" $Label $loser stick 1 0 4000 | Out-Null
        "$(Stamp) match $k walk-off by $loser"
        $seconds = [Math]::Max(30, $MatchSeconds - $walkDelay - 8)
    }
    & "$e/match.ps1" -Label $Label -Seconds $seconds -ShotSeconds 0 -Tag "$Tag-$k" | Out-Null
    "$(Stamp) match $k keepalive done"
    if ($Watch.Count -eq 0) {
        Start-Sleep -Seconds $PopupWait
        "$(Stamp) popup $k assumed after $PopupWait s"
    }
    else {
        $deadline = (Get-Date).AddSeconds(110)
        $seen = @{ s = ('s' -notin $Watch); y = ('y' -notin $Watch) }
        while ((Get-Date) -lt $deadline -and -not ($seen.s -and $seen.y)) {
            foreach ($i in $Watch) {
                if (-not $seen[$i] -and ((Lines $i) | Select-Object -Skip $before[$i] | Where-Object { $_ -match ' popup record=' })) { $seen[$i] = $true; "$(Stamp) popup $k on $i" }
            }
            Start-Sleep -Milliseconds 600
        }
        if (-not ($Watch | Where-Object { $seen[$_] })) { "$(Stamp) no popup after match $k; stopping"; break }
    }
    if ($GifName -and $k -eq 0) {
        $gif = @{ path = "$work/shots/$Label/$GifName"; fps = 12; width = 640; duration_ms = 15000 } | ConvertTo-Json -Compress
        "$(Stamp) gif: " + (/usr/bin/python3 -I "$e/drive.py" $Label s call recording.start $gif)
    }
    Start-Sleep -Milliseconds 1000
    Shot "popup$k"
    $press = $Answers[$k].Split('/')
    if ($press[0] -ne 'none') { /usr/bin/python3 -I "$e/drive.py" $Label s press $press[0] | Out-Null }
    if ($press[1] -ne 'none') { /usr/bin/python3 -I "$e/drive.py" $Label y press $press[1] | Out-Null }
    "$(Stamp) answered $($Answers[$k])"
    Start-Sleep -Seconds 9
    Shot "after$k"
}
Start-Sleep -Seconds 6
Shot 'end'
foreach ($i in 's', 'y') { "== $i"; Lines $i | Select-Object -Skip $start[$i] | Where-Object { $_ -notmatch ' diag (parsed|reparsed) ' } }
