[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Label,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9-]+$')][string]$Name,
    # Steps during the recording, ';'-separated: left-stick "x,y,ms,pause_ms"
    # or a button press "btn:<button>:pause_ms"; empty = none.
    [string]$Moves = '',
    [int]$DurationMs = 8000,
    [int]$Width = 1280,
    [int]$Fps = 10,
    [int]$LeadInMs = 1000
)
# Records a GIF with Eden's built-in recorder (recording.start) while the
# cursor moves over fighters on the character select.
$ErrorActionPreference = 'Stop'
$gv = Join-Path $PSScriptRoot 'gv.py'
$out = "/home/intpa/gsp-visualizer/eden/captures/$Label-$Name.gif"
New-Item -ItemType Directory -Force (Split-Path $out) | Out-Null
if (Test-Path $out) { throw "exists: $out" }
$params = @{ path = $out; fps = $Fps; width = $Width; duration_ms = $DurationMs } | ConvertTo-Json -Compress
/usr/bin/python3 -I $gv call $Label recording.start $params
Start-Sleep -Milliseconds $LeadInMs
if ($Moves) {
    foreach ($move in $Moves.Split(';')) {
        if ($move.StartsWith('btn:')) {
            $null, $button, $pause = $move.Split(':')
            /usr/bin/python3 -I $gv press $Label $button | Out-Null
        } else {
            $x, $y, $ms, $pause = $move.Split(',')
            /usr/bin/python3 -I $gv stick $Label $x $y $ms | Out-Null
        }
        Start-Sleep -Milliseconds ([int]$pause)
    }
}
$deadline = (Get-Date).AddMilliseconds($DurationMs + 30000)
do {
    Start-Sleep -Seconds 1
    $status = /usr/bin/python3 -I $gv call $Label recording.status | ConvertFrom-Json
} while ($status.state -eq 'recording' -and (Get-Date) -lt $deadline)
$status | ConvertTo-Json -Compress
if (Test-Path $out) { (Get-Item $out).Length }
