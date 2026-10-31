param([Parameter(Mandatory)][string]$Label)
# Title -> main menu -> Online -> Smash -> Quickplay -> Solo -> pick the
# fighter under the pointer, on both instances; screenshot at the end.
$e = $PSScriptRoot
function Both([string[]]$op) { foreach ($i in 's', 'y') { /usr/bin/python3 -I "$e/drive.py" $Label $i @op | Out-Null } }
foreach ($w in 7, 10) { Both @('press', 'a'); Start-Sleep -Seconds $w }
foreach ($s in @(@('down', 0.9), @('right', 0.9), @('a', 14), @('a', 6), @('a', 5), @('a', 9))) {
    Both @('press', $s[0]); Start-Sleep -Seconds $s[1]
}
Both @('stick', '0', '1', '450'); Start-Sleep -Milliseconds 500
Both @('press', 'a'); Start-Sleep -Seconds 3
$a = /usr/bin/python3 -I "$e/drive.py" $Label s shot nav
$b = /usr/bin/python3 -I "$e/drive.py" $Label y shot nav
/usr/bin/python3 -I "$e/thumb.py" "$e/shots/$Label/nav.jpg" $a $b
