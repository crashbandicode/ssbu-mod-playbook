param([Parameter(Mandatory)][ValidatePattern('^e\d{2,}$')][string]$Label,
      [string]$Steps, [string]$Name = 'step')
# Sends a sequence of inputs to one gv.py instance, then takes one screenshot.
# Steps: comma list of  b:<button>[+<button>][:<waitms>]  s:<x>:<y>:<ms>[:<waitms>]  w:<ms>
# (b:lstick+up is the tile cursor's L3 + Up: gv.py presses lstick first.)
$g = Join-Path $PSScriptRoot 'gv.py'
foreach ($step in ($Steps -split ',' | Where-Object { $_ })) {
    $p = $step -split ':'
    switch ($p[0]) {
        'b' { /usr/bin/python3 -I $g press $Label ($p[1] -replace '\+', ',') | Out-Null; Start-Sleep -Milliseconds ([int]($p[2] ?? 800)) }
        's' { /usr/bin/python3 -I $g stick $Label $p[1] $p[2] $p[3] | Out-Null; Start-Sleep -Milliseconds ([int]($p[4] ?? 600)) }
        'w' { Start-Sleep -Milliseconds ([int]$p[1]) }
    }
}
/usr/bin/python3 -I $g shot $Label $Name | Select-Object -Last 1
