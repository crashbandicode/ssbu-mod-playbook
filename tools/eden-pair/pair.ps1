[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Up', 'Down')][string]$Action,
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Label,
    [string]$DiagS,
    [string]$DiagY,
    [string]$DisplayS = ':64',
    [string]$DisplayY = ':66',
    [int]$StaggerSeconds = 30,
    [switch]$Shipping,
    # Leave the Yoga instance without elite-rematch (detector only).
    [switch]$NoPluginY,
    # Use the Eden agent's rating-storing LAN runtime (a verified copy at
    # $work/eden-lan-runtime on each host), optionally with per-slot seeds.
    [switch]$Runtime,
    [ValidatePattern('^(\d+:\d+(,\d+:\d+)*)?$')][string]$SeedS = '',
    [ValidatePattern('^(\d+:\d+(,\d+:\d+)*)?$')][string]$SeedY = ''
)
# Online pair for elite-rematch tests: Surface instance s + Yoga instance y over
# Eden LAN Online, each on its own private display and profile copy.
$ErrorActionPreference = 'Stop'
$surface = 'intpa@dreamland-surface.lan'
$work = '/home/intpa/elite-rematch-work'
$remoteTmp = "/tmp/elite-rematch-$Label"
$coord = '/tmp/deluxe-surface-1007/OWNER-COORDINATION.md'
$sockS = "/run/user/1000/elite-rematch-$Label-s.sock"
$helper = '/home/intpa/eden-nso-autolan/live-tools/display-rootful.ps1'
$owner = 'Claude 9311e6e4-66cd-4cd9-9aba-988997d3aa75'
$nro = if ($Shipping) { "$work/nro/libelite_rematch.nro" } else { "$work/nro/libelite_rematch.diag.nro" }
$detector = '/home/intpa/ssbu-desync-detector/target/aarch64-skyline-switch/release/libssbu_desync_detector.nro'
$games = @{
    s = '/home/intpa/eden-assets/games/01006A800016E000/base/Super Smash Bros. Ultimate [01006A800016E000]+[v13.0.3+ 9DLC][US](nsw2u.com).xci'
    y = '/home/intpa/Games/Roms/Switch/Super Smash Bros. Ultimate [01006A800016E000]+[v13.0.3+ 9DLC][US](nsw2u.com).xci'
}
$log = Join-Path $work "pair-$Label.log"

function Note([string]$text) {
    $line = "$((Get-Date).ToUniversalTime().ToString('HH:mm:ss'))Z $text"
    Add-Content -LiteralPath $log -Value $line
    $line
}
function Remote([string]$command) {
    $out = /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=15 $surface $command 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Surface command failed ($LASTEXITCODE): $command`n$out" }
    $out
}
function Stamp { (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm') }
function Drive([string]$inst, [string[]]$rest) {
    /usr/bin/python3 -I (Join-Path $PSScriptRoot 'drive.py') $Label $inst @rest
}

if ($Action -eq 'Up') {
    # Foreign claims: an owner whose last CLAIMED is newer than its last RELEASED.
    $active = @{}
    foreach ($line in (Get-Content -LiteralPath $coord -ErrorAction SilentlyContinue)) {
        if ($line -match '(CLAIMED|RELEASED): (\S+ \S+)') { $active[$Matches[2]] = ($Matches[1] -eq 'CLAIMED') }
    }
    $busy = $active.GetEnumerator() | Where-Object { $_.Value -and $_.Key -notmatch '9311e6e4' }
    if ($busy) { throw "Surface has an active claim: $($busy.Key -join ', ')" }
    # The emulator itself, not other agents' tools under eden-* folders.
    $foreign = Remote 'Get-Process | Where-Object { $_.ProcessName -match ''^(eden|yuzu)$'' -or $_.Path -like ''*/.mount_Eden*'' -or $_.Path -like ''*Eden*.AppImage'' } | Measure-Object | ForEach-Object Count'
    if ([int]"$foreign" -ne 0) { throw "Surface already runs $foreign Eden process(es)" }
    if (Test-Path "/tmp/.X11-unix/X$($DisplayY.Substring(1))") { throw "Yoga display $DisplayY in use" }
    if ("$(Remote "Test-Path /tmp/.X11-unix/X$($DisplayS.Substring(1))")" -eq 'True') { throw "Surface display $DisplayS in use" }
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z CLAIMED: $owner elite-rematch pair, Surface 192.168.0.236, display $DisplayS, run $Label, profile $work/profiles/s (+ Yoga $DisplayY, profile $work/profiles/y). No physical Switch writes.`n"
    Note "claimed Surface $DisplayS / Yoga $DisplayY for $Label nro=$nro"

    Remote "New-Item -ItemType Directory -Force $remoteTmp,$work | Out-Null" | Out-Null
    $files = @('prep.py', 'launch.py', 'stop.py' | ForEach-Object { Join-Path $PSScriptRoot $_ }) + @($nro, $detector)
    if ($DiagS) { $files += $DiagS }
    /usr/bin/scp -q @files "$($surface):$remoteTmp/"
    if ($LASTEXITCODE -ne 0) { throw 'scp to Surface failed' }
    $diagArgS = if ($DiagS) { "--diag-file $remoteTmp/$(Split-Path -Leaf $DiagS)" } else { '' }
    Note ("prep s: " + (Remote "/usr/bin/python3 -I $remoteTmp/prep.py $Label s $diagArgS $remoteTmp/$(Split-Path -Leaf $nro) $remoteTmp/libssbu_desync_detector.nro"))
    $diagArgY = if ($DiagY) { @('--diag-file', $DiagY) } else { @() }
    $nrosY = @(if ($NoPluginY) { $detector } else { $nro; $detector })
    Note ("prep y: " + (/usr/bin/python3 -I (Join-Path $PSScriptRoot 'prep.py') $Label y @diagArgY @nrosY))
    if ($LASTEXITCODE -ne 0) { throw 'prep y failed' }

    Remote "/usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Start -Unit erm-$Label-s-display -Socket wayland-erm-$Label-s -XDisplay $DisplayS -MaxRuntimeSeconds 5400" | Out-Null
    /usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Start -Unit "erm-$Label-y-display" -Socket "wayland-erm-$Label-y" -XDisplay $DisplayY -MaxRuntimeSeconds 5400 | Out-Null
    Note 'displays started'
    /usr/bin/systemd-run --user --quiet "--unit=erm-$Label-gpu-lock" --property=RuntimeMaxSec=5400 --property=TimeoutStopSec=5 /usr/bin/flock --nonblock /tmp/opencode/yoga-gpu-reservation.lock /usr/bin/sleep 5400
    Start-Sleep -Seconds 1
    if ("$(/usr/bin/systemctl --user is-active "erm-$Label-gpu-lock")" -ne 'active') { throw 'Yoga GPU lock is held by someone else' }

    $rtArgS = ''
    $rtArgsY = @()
    if ($Runtime) {
        $rt = "$work/eden-lan-runtime"
        $rtArgS = "$rt $SeedS"
        $rtArgsY = @($rt) + @(if ($SeedY) { $SeedY })
        Note "runtime $rt seeds s=$SeedS y=$SeedY"
    }
    Note ("launch s: " + (Remote "/usr/bin/python3 -I $remoteTmp/launch.py $Label s $DisplayS $rtArgS"))
    Note ("launch y: " + (/usr/bin/python3 -I (Join-Path $PSScriptRoot 'launch.py') $Label y $DisplayY @rtArgsY))
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        $remoteUp = "$(Remote "Test-Path $work/runs/$Label-s/control.sock")" -eq 'True'
        if ($remoteUp -and (Test-Path "$work/runs/$Label-y/control.sock")) { break }
        Start-Sleep -Seconds 3
    }
    if (Test-Path $sockS) { Remove-Item -LiteralPath $sockS }
    $fwd = Start-Process -FilePath /usr/bin/ssh -PassThru -RedirectStandardOutput /dev/null -RedirectStandardError "$work/fwd-$Label.err" -ArgumentList '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-L', "$($sockS):$work/runs/$Label-s/control.sock", $surface
    Set-Content -LiteralPath "$work/fwd-$Label.pid" -Value $fwd.Id
    Start-Sleep -Seconds 4
    # LAN Online's broker starts with the game and the first one leads the
    # coordinator; Surface first means the Yoga dials out to it (inbound
    # connections to the Yoga timed out in e01).
    foreach ($inst in 's', 'y') {
        Note ("game.load ${inst}: " + (Drive $inst @('call', 'game.load', (@{ path = $games[$inst] } | ConvertTo-Json -Compress))))
        if ($inst -eq 's') { Start-Sleep -Seconds $StaggerSeconds }
    }
    Note "up: forward pid $($fwd.Id)"
}
else {
    foreach ($inst in 's', 'y') {
        try { Drive $inst @('call', 'input.reset') | Out-Null } catch { }
    }
    try { Note ("stop s: " + (Remote "/usr/bin/python3 -I $remoteTmp/stop.py $Label s")) } catch { Note "stop s failed: $_" }
    try { Note ("stop y: " + (/usr/bin/python3 -I (Join-Path $PSScriptRoot 'stop.py') $Label y)) } catch { Note "stop y failed: $_" }
    try { Remote "/usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Stop -Unit erm-$Label-s-display -Socket wayland-erm-$Label-s -XDisplay $DisplayS -MaxRuntimeSeconds 5400" | Out-Null } catch { Note "display s stop failed: $_" }
    try { /usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Stop -Unit "erm-$Label-y-display" -Socket "wayland-erm-$Label-y" -XDisplay $DisplayY -MaxRuntimeSeconds 5400 | Out-Null } catch { Note "display y stop failed: $_" }
    /usr/bin/systemctl --user stop "erm-$Label-gpu-lock" 2>$null
    $pidFile = "$work/fwd-$Label.pid"
    if (Test-Path $pidFile) {
        Stop-Process -Id ([int](Get-Content $pidFile)) -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $pidFile
    }
    if (Test-Path $sockS) { Remove-Item -LiteralPath $sockS }
    $left = Remote 'Get-Process | Where-Object { $_.ProcessName -match ''^(eden|yuzu)$'' -or $_.Path -like ''*/.mount_Eden*'' -or $_.Path -like ''*Eden*.AppImage'' } | Measure-Object | ForEach-Object Count'
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z RELEASED: $owner elite-rematch pair stopped Eden run $Label, Surface display $DisplayS and its SSH control forward (Yoga $DisplayY too); profile $work/profiles/s retained. No physical Switch writes.`n"
    Note "down; Surface Eden processes left: $left"
}
