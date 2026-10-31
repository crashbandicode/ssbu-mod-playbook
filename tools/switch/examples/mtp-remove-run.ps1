param([Parameter(Mandatory)][ValidateSet('preflight', 'remove')][string]$Mode)
# Runs one remove-mtp.py phase for shaderdb-probe on the Surface (single
# writer), sending the remote script over ssh stdin.
$remote = @"
`$ErrorActionPreference = 'Continue'
Set-Location /home/intpa/gsp-visualizer-install/switch-mtp-remove-shaderdb
`$o = /usr/bin/python3 -I remove-mtp.py $Mode shaderdb 2>&1
"exit=`$LASTEXITCODE"
`$o | Select-Object -Last 8
"@
$remote | ssh -o BatchMode=yes -o ConnectTimeout=10 intpa@dreamland-surface.lan '/usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -Command -' 2>&1 |
    ForEach-Object { "$_".Substring(0, [Math]::Min(220, "$_".Length)) }
