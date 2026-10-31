param([Parameter(Mandatory)][ValidateSet('preflight', 'install')][string]$Mode,
      [Parameter(Mandatory)][ValidateSet('elite-rematch', 'desync', 'deluxe', 'refresh-clock', 'shaderdb')][string]$Folder)
# Runs one upgrade2-mtp.py phase for one plugin on the Surface (single writer),
# sending the remote script over ssh stdin, and prints the result lines.
$remote = @"
`$ErrorActionPreference = 'Continue'
Set-Location /home/intpa/gsp-visualizer-install/switch-mtp-vcut-1031
`$o = /usr/bin/python3 -I upgrade2-mtp.py $Mode $Folder 2>&1
"exit=`$LASTEXITCODE"
`$o | Select-Object -Last 8
"@
$remote | ssh -o BatchMode=yes -o ConnectTimeout=10 intpa@dreamland-surface.lan '/usr/bin/pwsh -NoLogo -NoProfile -NonInteractive -Command -' 2>&1 |
    ForEach-Object { "$_".Substring(0, [Math]::Min(220, "$_".Length)) }
