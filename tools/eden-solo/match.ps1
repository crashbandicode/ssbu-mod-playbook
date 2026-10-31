[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Winner,
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Loser,
    [int]$Seconds = 230
)
# One real Quickplay match: the winner holds still (keepalive), the loser
# walks off the stage every few seconds (self-destructs), so the winner wins
# on score. Screenshots of both instances afterwards.
$ErrorActionPreference = 'Stop'
$g = Join-Path $PSScriptRoot 'gv.py'
$stop = "/home/intpa/gsp-visualizer-work/keepalive-$Winner.stop"
Remove-Item $stop -ErrorAction SilentlyContinue
$keep = Start-Process -FilePath /usr/bin/python3 -PassThru -RedirectStandardOutput "/home/intpa/gsp-visualizer-work/keepalive-$Winner.out" -RedirectStandardError /dev/null -ArgumentList '-I', $g, 'keepalive', $Winner, $stop, "$Seconds"
$end = (Get-Date).AddSeconds($Seconds)
$walks = 0
while ((Get-Date) -lt $end) {
    $direction = if ($walks % 2) { -1 } else { 1 }
    /usr/bin/python3 -I $g stick $Loser $direction 0 3500 | Out-Null
    $walks++
    Start-Sleep -Milliseconds 2500
}
New-Item -ItemType File $stop -Force | Out-Null
$keep | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue
"walks=$walks keepalive=" + (Get-Content "/home/intpa/gsp-visualizer-work/keepalive-$Winner.out" -ErrorAction SilentlyContinue)
/usr/bin/python3 -I $g shot $Winner after
/usr/bin/python3 -I $g shot $Loser after
