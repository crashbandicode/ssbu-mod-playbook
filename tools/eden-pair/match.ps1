param([Parameter(Mandatory)][string]$Label, [int]$Seconds = 128, [int]$ShotSeconds = 48, [string]$Tag = 'm')
# Keep both fighters active for the match, then screenshot both instances
# every ~4 s through the results and the post-match popup.
$e = $PSScriptRoot
$stop = "/home/intpa/elite-rematch-work/keepalive-$Label-$Tag.stop"
$procs = foreach ($i in 's', 'y') {
    Start-Process -FilePath /usr/bin/python3 -PassThru -RedirectStandardOutput "/home/intpa/elite-rematch-work/keepalive-$Label-$Tag-$i.out" -RedirectStandardError /dev/null -ArgumentList '-I', "$e/drive.py", $Label, $i, 'keepalive', $stop, "$Seconds"
}
$procs | Wait-Process -Timeout ($Seconds + 30) -ErrorAction SilentlyContinue
"keepalive done $((Get-Date).ToUniversalTime().ToString('HH:mm:ss'))Z"
$end = (Get-Date).AddSeconds($ShotSeconds)
$n = 0
while ((Get-Date) -lt $end) {
    $n++
    foreach ($i in 's', 'y') { /usr/bin/python3 -I "$e/drive.py" $Label $i shot "$Tag-$('{0:d2}' -f $n)" }
    Start-Sleep -Seconds 2
}
