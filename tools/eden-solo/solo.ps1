[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Up', 'Down')][string]$Action,
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Label,
    [string[]]$Nro = @('/home/intpa/gsp-visualizer/dist/diag/libgsp_visualizer.nro'),
    [string]$Diag,
    [ValidatePattern('^(\d+:\d+(,\d+:\d+)*)?$')][string]$Seeds = ''
)
# One gsp-visualizer Eden instance on this Yoga (display :68) with the Eden
# agent's rating runtime, for the character select label. Adapted from
# /home/intpa/elite-rematch/eden/local_pair.ps1.
$ErrorActionPreference = 'Stop'
# -File passes a comma list as one string.
$Nro = @($Nro | ForEach-Object { $_ -split ',' } | Where-Object { $_ -and $_ -ne 'none' })
$work = '/home/intpa/gsp-visualizer-work'
$coord = '/tmp/deluxe-surface-1007/OWNER-COORDINATION.md'
$helper = '/home/intpa/eden-nso-autolan/live-tools/display-rootful.ps1'
$owner = 'Claude 88c0f449-28a7-479d-ad98-1721e2fad049'
$game = '/home/intpa/Games/Roms/Switch/Super Smash Bros. Ultimate [01006A800016E000]+[v13.0.3+ 9DLC][US](nsw2u.com).xci'
$display = ':68'
$gv = Join-Path $PSScriptRoot 'gv.py'
New-Item -ItemType Directory -Force $work | Out-Null
$log = Join-Path $work "solo-$Label.log"
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
    $busy = $active.GetEnumerator() | Where-Object { $_.Value -and $_.Key -notmatch '88c0f449' }
    if ($busy) { throw "active claim: $($busy.Key -join ', ')" }
    $here = (Get-Process | Where-Object { $_.ProcessName -match '^(eden|yuzu|eden-lan-runtime)$' } | Measure-Object).Count
    if ($here -ne 0) { throw "this host already runs $here Eden process(es)" }
    if (Test-Path "/tmp/.X11-unix/X$($display.Substring(1))") { throw "display $display in use" }
    $mem = [long]((Get-Content /proc/meminfo | Select-String '^MemAvailable:').Line -split '\s+')[1]
    if ($mem -lt 10 * 1024 * 1024) { throw "MemAvailable $mem KiB < 10 GiB" }
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z CLAIMED: $owner gsp-visualizer solo instance on Yoga, display $display, run $Label, profile $work/profiles/y (copy of ssbu-desync-1029/profiles/b), Eden agent runtime v8 copy, TLS on. No Surface use; please no other LAN Online instances until RELEASED. No physical Switch writes.`n"
    Note "claimed solo $Label nro=$($Nro -join ',') seeds=$Seeds"
    $diagArgs = if ($Diag) { @('--diag-file', $Diag) } else { @() }
    Note ("prep: " + (Py (@($gv, 'prep', $Label) + $diagArgs + $Nro)))
    /usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Start -Unit "gv-$Label-display" -Socket "wayland-gv-$Label" -XDisplay $display -MaxRuntimeSeconds 5400 | Out-Null
    Note 'display started'
    /usr/bin/systemd-run --user --quiet "--unit=gv-$Label-gpu-lock" --property=RuntimeMaxSec=5400 --property=TimeoutStopSec=5 /usr/bin/flock --nonblock /tmp/opencode/yoga-gpu-reservation.lock /usr/bin/sleep 5400
    Start-Sleep -Seconds 1
    if ("$(/usr/bin/systemctl --user is-active "gv-$Label-gpu-lock")" -ne 'active') { throw 'Yoga GPU lock is held by someone else' }
    Note ("launch: " + (Py (@($gv, 'launch', $Label, $display) + @(if ($Seeds) { $Seeds }))))
    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Test-Path "$work/runs/$Label/control.sock") -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 2 }
    Note ("game.load: " + (Py @($gv, 'call', $Label, 'game.load', (@{ path = $game } | ConvertTo-Json -Compress))))
    Note 'up'
}
else {
    try { Note ("stop: " + (Py @($gv, 'stop', $Label))) } catch { Note "stop failed: $_" }
    try { /usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Stop -Unit "gv-$Label-display" -Socket "wayland-gv-$Label" -XDisplay $display -MaxRuntimeSeconds 5400 | Out-Null } catch { Note "display stop failed: $_" }
    /usr/bin/systemctl --user stop "gv-$Label-gpu-lock" 2>$null
    $left = (Get-Process | Where-Object { $_.ProcessName -match '^(eden|yuzu|eden-lan-runtime)$' } | Measure-Object).Count
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z RELEASED: $owner gsp-visualizer solo instance stopped Eden run $Label and Yoga display $display; profile retained. No physical Switch writes.`n"
    Note "down; Eden processes left here: $left"
}
