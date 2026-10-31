[CmdletBinding()]
param([Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Label, [int]$BootSeconds = 0)
# Title → Online → Smash → Quickplay → Solo character select, then one
# tile-cursor step up so the hand (carrying P1's token) is on a fighter.
$ErrorActionPreference = 'Stop'
$g = Join-Path $PSScriptRoot 'gv.py'
function P([string]$b, [int]$ms = 120) { /usr/bin/python3 -I $g press $Label $b $ms | Out-Null }
function S([double]$x, [double]$y, [int]$ms) { /usr/bin/python3 -I $g stick $Label $x $y $ms | Out-Null }
Start-Sleep -Seconds $BootSeconds
P a; Start-Sleep 4; P a; Start-Sleep 6
S 0 -1 250; Start-Sleep -Milliseconds 600; S 1 0 250; Start-Sleep 1
P a; Start-Sleep 12   # Online
P a; Start-Sleep 12   # Smash
P a; Start-Sleep 15   # Quickplay
P a; Start-Sleep 15   # Solo
P 'lstick,up' 150; Start-Sleep 2
/usr/bin/python3 -I $g shot $Label css
