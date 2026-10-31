[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Up', 'Down')][string]$Action,
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Label,
    # The instance's whole plugin set (local paths).
    [string[]]$Nros = @(),
    [string]$Diag,
    [string]$Display = ':64'
)
# One offline Surface instance (LAN Online off, airplane mode on) for boot
# checks: which plugins install and how; no network, so no LAN Online peer.
$ErrorActionPreference = 'Stop'
$surface = 'intpa@dreamland-surface.lan'
$work = '/home/intpa/elite-rematch-work'
$remoteTmp = "/tmp/elite-rematch-$Label"
$coord = '/tmp/deluxe-surface-1007/OWNER-COORDINATION.md'
$sock = "/run/user/1000/elite-rematch-$Label-s.sock"
$helper = '/home/intpa/eden-nso-autolan/live-tools/display-rootful.ps1'
$owner = 'Claude 9311e6e4-66cd-4cd9-9aba-988997d3aa75'
$game = '/home/intpa/eden-assets/games/01006A800016E000/base/Super Smash Bros. Ultimate [01006A800016E000]+[v13.0.3+ 9DLC][US](nsw2u.com).xci'
$log = Join-Path $work "solo-$Label.log"
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
$countEden = 'Get-Process | Where-Object { $_.ProcessName -match ''^(eden|yuzu)$'' -or $_.Path -like ''*/.mount_Eden*'' -or $_.Path -like ''*Eden*.AppImage'' } | Measure-Object | ForEach-Object Count'

if ($Action -eq 'Up') {
    if (-not $Nros.Count) { throw 'no plugins given' }
    $foreign = Remote $countEden
    if ([int]"$foreign" -ne 0) { throw "Surface already runs $foreign Eden process(es)" }
    if ("$(Remote "Test-Path /tmp/.X11-unix/X$($Display.Substring(1))")" -eq 'True') { throw "Surface display $Display in use" }
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z CLAIMED: $owner elite-rematch solo OFFLINE boot check on Surface 192.168.0.236 (LAN Online off, airplane mode on), display $Display, run $Label, profile $work/profiles/s. Not a LAN Online instance. No physical Switch writes.`n"
    Note "claimed Surface $Display for $Label plugins=$($Nros.Count)"

    Remote "New-Item -ItemType Directory -Force $remoteTmp/nro,$work | Out-Null" | Out-Null
    /usr/bin/scp -q @('prep.py', 'launch.py', 'stop.py' | ForEach-Object { Join-Path $PSScriptRoot $_ }) "$($surface):$remoteTmp/"
    /usr/bin/scp -q @Nros "$($surface):$remoteTmp/nro/"
    if ($LASTEXITCODE -ne 0) { throw 'scp to Surface failed' }
    $diagArg = ''
    if ($Diag) {
        /usr/bin/scp -q $Diag "$($surface):$remoteTmp/diag.txt"
        $diagArg = "--diag-file $remoteTmp/diag.txt"
    }
    $remoteNros = ($Nros | ForEach-Object { "$remoteTmp/nro/$(Split-Path -Leaf $_)" }) -join ' '
    Note ("prep s: " + (Remote "/usr/bin/python3 -I $remoteTmp/prep.py $Label s --offline $diagArg $remoteNros"))
    Remote "/usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Start -Unit erm-$Label-s-display -Socket wayland-erm-$Label-s -XDisplay $Display -MaxRuntimeSeconds 3600" | Out-Null
    Note 'display started'
    Note ("launch s: " + (Remote "/usr/bin/python3 -I $remoteTmp/launch.py $Label s $Display"))
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline -and "$(Remote "Test-Path $work/runs/$Label-s/control.sock")" -ne 'True') { Start-Sleep -Seconds 3 }
    if (Test-Path $sock) { Remove-Item -LiteralPath $sock }
    $fwd = Start-Process -FilePath /usr/bin/ssh -PassThru -RedirectStandardOutput /dev/null -RedirectStandardError "$work/fwd-$Label.err" -ArgumentList '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-L', "$($sock):$work/runs/$Label-s/control.sock", $surface
    Set-Content -LiteralPath "$work/fwd-$Label.pid" -Value $fwd.Id
    Start-Sleep -Seconds 4
    Note ("game.load s: " + (/usr/bin/python3 -I (Join-Path $PSScriptRoot 'drive.py') $Label s call game.load (@{ path = $game } | ConvertTo-Json -Compress)))
    Note "up: forward pid $($fwd.Id)"
}
else {
    try { Note ("stop s: " + (Remote "/usr/bin/python3 -I $remoteTmp/stop.py $Label s")) } catch { Note "stop s failed: $_" }
    try { Remote "/usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -File $helper -Action Stop -Unit erm-$Label-s-display -Socket wayland-erm-$Label-s -XDisplay $Display -MaxRuntimeSeconds 3600" | Out-Null } catch { Note "display stop failed: $_" }
    $pidFile = "$work/fwd-$Label.pid"
    if (Test-Path $pidFile) {
        Stop-Process -Id ([int](Get-Content $pidFile)) -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $pidFile
    }
    if (Test-Path $sock) { Remove-Item -LiteralPath $sock }
    $left = Remote $countEden
    Add-Content -LiteralPath $coord -Value "`n$(Stamp)Z RELEASED: $owner elite-rematch solo OFFLINE boot check stopped Eden run $Label and Surface display $Display; profile $work/profiles/s retained. No physical Switch writes.`n"
    Note "down; Surface Eden processes left: $left"
}
