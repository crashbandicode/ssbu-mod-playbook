[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Up', 'Down')][string]$Action,
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Label,
    [string]$DiagS,
    [ValidatePattern('^(\d+:\d+(,\d+:\d+)*)?$')][string]$SeedS = '',
    [ValidatePattern('^(\d+:\d+(,\d+:\d+)*)?$')][string]$SeedY = '',
    [switch]$Shipping,
    # The modded instance's whole plugin set instead of our NRO + detector
    # (e.g. a copy of the Switch card's plugins folder).
    [string[]]$NroS = @(),
    [string]$Detector = '/home/intpa/ssbu-desync-detector/target/aarch64-skyline-switch/release/libssbu_desync_detector.nro',
    [int]$StaggerSeconds = 30
)
# Same-PC pair on this Yoga (Eden supports it): instance s = modded (elite-rematch
# + detector), instance y = vanilla (detector only), both with the Eden agent's
# rating runtime and their own seeds, TLS on so they only pair with each other.
$ErrorActionPreference = 'Stop'
$work = '/home/intpa/elite-rematch-work'
$coord = '/tmp/deluxe-surface-1007/OWNER-COORDINATION.md'
$helper = '/home/intpa/eden-nso-autolan/live-tools/display-rootful.ps1'
$owner = 'Claude 9311e6e4-66cd-4cd9-9aba-988997d3aa75'
$nro = if ($Shipping) { "$work/nro/libelite_rematch.nro" } else { "$work/nro/libelite_rematch.diag.nro" }
$detector = $Detector
$pluginsS = if ($NroS.Count) { $NroS } else { @($nro, $detector) }
$runtime = "$work/eden-lan-runtime"
$game = '/home/intpa/Games/Roms/Switch/Super Smash Bros. Ultimate [01006A800016E000]+[v13.0.3+ 9DLC][US](nsw2u.com).xci'
$displays = @{ s = ':67'; y = ':66' }
$log = Join-Path $work "pair-$Label.log"
function Note([string]$text) {
    $line = "$((Get-Date).ToUniversalTime().ToString('HH:mm:ss'))Z $text"
    Add-Content -LiteralPath $log -Value $line
    $line
}
function Stamp { (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm') }
function Py([string[]]$arguments) {
    $out = /usr/bin/python3 -I @arguments 2>&1
    if ($LASTEXITCODE -ne 0) { throw "python failed: $($arguments -join ' ')`n$out" }
    "$out"
}

if ($Action -eq 'Up') {
    $active = @{}
    foreach ($line in (Get-Content -LiteralPath $coord -ErrorAction SilentlyContinue)) {
        if ($line -match '(CLAIMED|RELEASED): (\S+ \S+)') { $active[$Matches[2]] = ($Matches[1] -eq 'CLAIMED') }
    }
    $busy = $active.GetEnumerator() | Where-Object { $_.Value -and $_.Key -notmatch '9311e6e4' }
    if ($busy) { throw "active claim: $($busy.Key -join ', ')" }
    $here = (Get-Process | Where-Object { $_.ProcessName -match '^(eden|yuzu|eden-lan-runtime)$' } | Measure-Object).Count
    if ($here -ne 0) { throw "this host already runs $here Eden process(es)" }
    foreach ($d in $displays.Values) { if (Test-Path "/tmp/.X11-unix/X$($d.Substring(1))") { throw "display $d in use" } }
    $want = 'ee3c2e953a0b87541c29eb8918ce0354489035296ac5eaf060b3444d06a8bbcb'
    if ((Get-FileHash -Algorithm SHA256 $runtime).Hash.ToLower() -ne $want) { throw 'runtime hash mismatch' }
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z CLAIMED: $owner elite-rematch same-PC pair on Yoga, displays :66/:67, run $Label, profiles $work/profiles/{s,y} (Yoga), Eden agent runtime v8 copy. No Surface use. No physical Switch writes.`n"
    Note "claimed same-PC pair $Label s=$($pluginsS -join ',') seeds s=$SeedS y=$SeedY"

    $diagArgs = if ($DiagS) { @('--diag-file', $DiagS) } else { @() }
    Note ("prep s: " + (Py (@((Join-Path $PSScriptRoot 'prep.py'), $Label, 's', '--source', "$work/profiles/y", '--tls', 'true') + $diagArgs + $pluginsS)))
    Note ("prep y: " + (Py @((Join-Path $PSScriptRoot 'prep.py'), $Label, 'y', '--tls', 'true', $detector)))
    foreach ($i in 's', 'y') {
        /usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Start -Unit "erm-$Label-$i-display" -Socket "wayland-erm-$Label-$i" -XDisplay $displays[$i] -MaxRuntimeSeconds 5400 | Out-Null
    }
    Note 'displays started'
    /usr/bin/systemd-run --user --quiet "--unit=erm-$Label-gpu-lock" --property=RuntimeMaxSec=5400 --property=TimeoutStopSec=5 /usr/bin/flock --nonblock /tmp/opencode/yoga-gpu-reservation.lock /usr/bin/sleep 5400
    Start-Sleep -Seconds 1
    if ("$(/usr/bin/systemctl --user is-active "erm-$Label-gpu-lock")" -ne 'active') { throw 'Yoga GPU lock is held by someone else' }
    foreach ($i in 's', 'y') {
        $seeds = if ($i -eq 's') { $SeedS } else { $SeedY }
        Note ("launch ${i}: " + (Py (@((Join-Path $PSScriptRoot 'launch.py'), $Label, $i, $displays[$i], $runtime) + @(if ($seeds) { $seeds }))))
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-Path "$work/runs/$Label-$i/control.sock") -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
        Note ("game.load ${i}: " + (Py @((Join-Path $PSScriptRoot 'drive.py'), $Label, $i, 'call', 'game.load', (@{ path = $game } | ConvertTo-Json -Compress))))
        # The first broker leads the coordinator; give it time before the second.
        if ($i -eq 's') { Start-Sleep -Seconds $StaggerSeconds }
    }
    Note 'up'
}
else {
    foreach ($i in 's', 'y') {
        try { Note ("stop ${i}: " + (Py @((Join-Path $PSScriptRoot 'stop.py'), $Label, $i))) } catch { Note "stop $i failed: $_" }
        try { /usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Stop -Unit "erm-$Label-$i-display" -Socket "wayland-erm-$Label-$i" -XDisplay $displays[$i] -MaxRuntimeSeconds 5400 | Out-Null } catch { Note "display $i stop failed: $_" }
    }
    /usr/bin/systemctl --user stop "erm-$Label-gpu-lock" 2>$null
    $left = (Get-Process | Where-Object { $_.ProcessName -match '^(eden|yuzu|eden-lan-runtime)$' } | Measure-Object).Count
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z RELEASED: $owner elite-rematch same-PC pair stopped Eden run $Label and Yoga displays :66/:67; profiles retained. No physical Switch writes.`n"
    Note "down; Eden processes left here: $left"
}
