[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Up', 'Down')][string]$Action,
    # Instance A (profile y, display :68) and B (profile s, display :69).
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$LabelA,
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$LabelB,
    [string]$NroA = '/home/intpa/gsp-visualizer/dist/release/libgsp_visualizer.nro',
    [ValidatePattern('^(\d+:\d+(,\d+:\d+)*)?$')][string]$SeedA = '',
    [int]$StaggerSeconds = 30
)
# Same-PC pair on this Yoga for a real Quickplay match (adapted from
# /home/intpa/elite-rematch/eden/local_pair.ps1). Both instances use the Eden
# agent's rating runtime and TLS, so they only pair with each other; both
# carry the tile-cursor plugin for exact fighter picks.
$ErrorActionPreference = 'Stop'
$work = '/home/intpa/gsp-visualizer-work'
$coord = '/tmp/deluxe-surface-1007/OWNER-COORDINATION.md'
$helper = '/home/intpa/eden-nso-autolan/live-tools/display-rootful.ps1'
$owner = 'Claude 88c0f449-28a7-479d-ad98-1721e2fad049'
$tile = '/home/intpa/ssbu-tile-cursor/dist/libssbu_tile_cursor.nro'
$game = '/home/intpa/Games/Roms/Switch/Super Smash Bros. Ultimate [01006A800016E000]+[v13.0.3+ 9DLC][US](nsw2u.com).xci'
$gv = Join-Path $PSScriptRoot 'gv.py'
$inst = @(
    @{ label = $LabelA; profile = 'y'; display = ':68'; nros = @($NroA, $tile); seeds = $SeedA },
    @{ label = $LabelB; profile = 's'; display = ':69'; nros = @($tile); seeds = '' }
)
$log = Join-Path $work "pair-$LabelA.log"
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
function MemOk { [long]((Get-Content /proc/meminfo | Select-String '^MemAvailable:').Line -split '\s+')[1] -ge 10 * 1024 * 1024 }

if ($Action -eq 'Up') {
    $active = @{}
    foreach ($line in (Get-Content -LiteralPath $coord -ErrorAction SilentlyContinue)) {
        if ($line -match '(CLAIMED|RELEASED): (\S+ \S+)') { $active[$Matches[2]] = ($Matches[1] -eq 'CLAIMED') }
    }
    $busy = $active.GetEnumerator() | Where-Object { $_.Value -and $_.Key -notmatch '88c0f449' }
    if ($busy) { throw "active claim: $($busy.Key -join ', ')" }
    $here = (Get-Process | Where-Object { $_.ProcessName -match '^(eden|yuzu|eden-lan-runtime)$' } | Measure-Object).Count
    if ($here -ne 0) { throw "this host already runs $here Eden process(es)" }
    foreach ($i in $inst) { if (Test-Path "/tmp/.X11-unix/X$($i.display.Substring(1))") { throw "display $($i.display) in use" } }
    if (-not (MemOk)) { throw 'MemAvailable < 10 GiB' }
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z CLAIMED: $owner gsp-visualizer same-PC pair on Yoga, displays :68/:69, runs $LabelA/$LabelB, profiles $work/profiles/{y,s}, Eden agent runtime v8 copy, TLS on. No Surface use; please no other LAN Online instances until RELEASED. No physical Switch writes.`n"
    Note "claimed pair $LabelA/$LabelB seedA=$SeedA"
    foreach ($i in $inst) {
        Note ("prep $($i.label): " + (Py (@($gv, 'prep', $i.label, '--profile', $i.profile) + $i.nros)))
        /usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Start -Unit "gv-$($i.label)-display" -Socket "wayland-gv-$($i.label)" -XDisplay $i.display -MaxRuntimeSeconds 5400 | Out-Null
    }
    Note 'displays started'
    /usr/bin/systemd-run --user --quiet "--unit=gv-$LabelA-gpu-lock" --property=RuntimeMaxSec=5400 --property=TimeoutStopSec=5 /usr/bin/flock --nonblock /tmp/opencode/yoga-gpu-reservation.lock /usr/bin/sleep 5400
    Start-Sleep -Seconds 1
    if ("$(/usr/bin/systemctl --user is-active "gv-$LabelA-gpu-lock")" -ne 'active') { throw 'Yoga GPU lock is held by someone else' }
    foreach ($i in $inst) {
        if (-not (MemOk)) { throw "MemAvailable < 10 GiB before launching $($i.label)" }
        Note ("launch $($i.label): " + (Py (@($gv, 'launch', $i.label, '--profile', $i.profile, $i.display) + @(if ($i.seeds) { $i.seeds }))))
        $deadline = (Get-Date).AddSeconds(90)
        while (-not (Test-Path "$work/runs/$($i.label)/control.sock") -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
        Note ("game.load $($i.label): " + (Py @($gv, 'call', $i.label, 'game.load', (@{ path = $game } | ConvertTo-Json -Compress))))
        # The first broker leads the coordinator; give it time before the second.
        if ($i.label -eq $LabelA) { Start-Sleep -Seconds $StaggerSeconds }
    }
    Note 'up'
}
else {
    foreach ($i in $inst) {
        try { Note ("stop $($i.label): " + (Py @($gv, 'stop', $i.label))) } catch { Note "stop $($i.label) failed: $_" }
        try { /usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Stop -Unit "gv-$($i.label)-display" -Socket "wayland-gv-$($i.label)" -XDisplay $i.display -MaxRuntimeSeconds 5400 | Out-Null } catch { Note "display $($i.label) stop failed: $_" }
    }
    /usr/bin/systemctl --user stop "gv-$LabelA-gpu-lock" 2>$null
    $left = (Get-Process | Where-Object { $_.ProcessName -match '^(eden|yuzu|eden-lan-runtime)$' } | Measure-Object).Count
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z RELEASED: $owner gsp-visualizer same-PC pair stopped Eden runs $LabelA/$LabelB and Yoga displays :68/:69; profiles retained. No physical Switch writes.`n"
    Note "down; Eden processes left here: $left"
}
