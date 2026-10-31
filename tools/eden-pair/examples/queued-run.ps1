$ErrorActionPreference = 'Stop'
# A queued Eden run (elite-rematch e26): waits until the Yoga is free, claims
# it through local_pair.ps1, runs the route and the test, prints the logs and
# always runs Down. Start it in the background, bounded, output to a file.
$e = '/home/intpa/elite-rematch/eden'
$work = '/home/intpa/elite-rematch-work'
$card = "$work/card-20261030"
$w = "$work/nro"
$coord = '/tmp/deluxe-surface-1007/OWNER-COORDINATION.md'
$label = 'e26'
function Stamp { (Get-Date).ToUniversalTime().ToString('HH:mm:ss') + 'Z' }
function Free {
    $active = @{}
    foreach ($line in Get-Content $coord) {
        if ($line -match '(CLAIMED|RELEASED): (\S+ \S+)') { $active[$Matches[2]] = ($Matches[1] -eq 'CLAIMED') }
    }
    $busy = $active.GetEnumerator() | Where-Object { $_.Value -and $_.Key -notmatch '9311e6e4' }
    $eden = (Get-Process | Where-Object { $_.ProcessName -match '^(eden|yuzu|eden-lan-runtime)$' } | Measure-Object).Count
    $kb = [long]((Select-String -Path /proc/meminfo -Pattern '^MemAvailable:\s+(\d+)').Matches[0].Groups[1].Value)
    (-not $busy) -and $eden -eq 0 -and $kb -ge 10GB / 1KB
}
$nros = @((Get-ChildItem "$card/*.nro" | Where-Object Name -ne 'libelite_rematch.nro' | Sort-Object Name).FullName) + @("$w/libelite_rematch.nro")
$deadline = (Get-Date).AddMinutes(90)
$up = $false
while (-not $up) {
    while (-not (Free)) {
        if ((Get-Date) -gt $deadline) { "$(Stamp) gave up waiting for the Yoga"; exit 1 }
        Start-Sleep -Seconds 10
    }
    try {
        & "$e/local_pair.ps1" -Action Up -Label $label -NroS $nros -Detector "$card/libssbu_desync_detector.nro" -SeedS '43:1641' -SeedY '43:1500' | Out-Null
        $up = $true
    }
    catch {
        "$(Stamp) up failed: $_"
        if ("$_" -notmatch 'active claim|already runs') { exit 1 }
        Start-Sleep -Seconds 10
    }
}
"$(Stamp) $label up"
$u = "$work/profiles/s/user/sdmc/ultimate"
try {
    for ($i = 0; $i -lt 40 -and -not (Test-Path "$u/elite-rematch/events.log"); $i++) { Start-Sleep -Seconds 5 }
    Start-Sleep -Seconds 60
    & "$e/nav.ps1" -Label $label | Out-Null
    & "$e/chain.ps1" -Label $label -Tag c1 -Answers @('a/a', 'b/b') -Losers @('y', 'y') -Search -DetectStart -Watch @() -PopupWait 20 | ForEach-Object { "  $_" }
    Start-Sleep -Seconds 3
}
catch { "$(Stamp) run failed: $_" }
"== elite-rematch events.log"
Get-Content "$u/elite-rematch/events.log" -ErrorAction SilentlyContinue
"== detector"
foreach ($i in 's', 'y') {
    Get-ChildItem "$work/profiles/$i/user/sdmc/ultimate/ssbu-desync-detector" -Recurse -File -ErrorAction SilentlyContinue |
        Select-String -Pattern 'session slot=|sync_end' | ForEach-Object { "$i $($_.Line)" }
}
"== shots"
Get-ChildItem "$e/shots/$label" -Filter '*popup*' -ErrorAction SilentlyContinue | ForEach-Object FullName
& "$e/local_pair.ps1" -Action Down -Label $label | Select-Object -Last 1
"$(Stamp) done"
