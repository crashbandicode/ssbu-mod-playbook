# Creates the three Forgejo releases for tags already pushed, uploads their
# assets and verifies each asset by an anonymous download. The credential comes
# from git's credential helper and is never printed or written.
$ErrorActionPreference = 'Stop'
$api = 'https://git.syntaxbyexample.com/api/v1/repos/intpa'
$w = '/home/intpa/gsp-visualizer-work'
$deluxe = "$w/deluxe-090/a068094b40f2a584"
$plan = @(
    @{ repo = 'ssbu-desync-detector'; tag = 'v0.2.0'; name = 'ssbu-desync-detector v0.2.0'; pre = $false
       assets = @('libssbu_desync_detector.nro', 'ssbu-desync-detector-v0.2.0-sd.zip', 'manifest.json', 'SHA256SUMS.txt') | ForEach-Object { "$w/release-assets/ssbu-desync-detector/$_" } },
    @{ repo = 'ssbu-refresh-clock'; tag = 'v0.1.0'; name = 'v0.1.0 — console-confirmed correction'; pre = $true
       assets = @('libssbu_refresh_clock.nro', 'ssbu-refresh-clock-v0.1.0-sd.zip', 'manifest.json', 'SHA256SUMS.txt') | ForEach-Object { "$w/release-assets/ssbu-refresh-clock/$_" } },
    @{ repo = 'ssbu-online-deluxe-rewrite'; tag = 'v0.9.0'; name = 'v0.9.0'; pre = $false
       assets = @("$deluxe/libssbu_online_deluxe_rewrite.nro", "$deluxe/manifest.json") }
)
$cred = "protocol=https`nhost=git.syntaxbyexample.com`n`n" | git credential fill
$user = (($cred | Where-Object { $_ -like 'username=*' }) -replace '^username=', '')
$secret = (($cred | Where-Object { $_ -like 'password=*' }) -replace '^password=', '')
if (-not $user -or -not $secret) { throw 'no Forgejo credential from the helper' }
$headers = @{ Authorization = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${user}:$secret")) }
Remove-Variable secret, cred
foreach ($r in $plan) {
    foreach ($a in $r.assets) { if (-not (Test-Path -LiteralPath $a)) { throw "missing asset $a" } }
    $exists = $true
    try { Invoke-RestMethod -TimeoutSec 20 "$api/$($r.repo)/releases/tags/$($r.tag)" | Out-Null } catch { $exists = $false }
    if ($exists) { throw "$($r.repo) already has a release for $($r.tag); inspect first" }
    $body = [IO.File]::ReadAllText("$w/release-assets/$($r.repo)-body.md")
    $json = @{ tag_name = $r.tag; name = $r.name; body = $body; draft = $false; prerelease = $r.pre } | ConvertTo-Json
    $release = Invoke-RestMethod -TimeoutSec 60 -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' `
        -Uri "$api/$($r.repo)/releases" -Body ([Text.Encoding]::UTF8.GetBytes($json))
    foreach ($a in $r.assets) {
        $leaf = Split-Path $a -Leaf
        Invoke-RestMethod -TimeoutSec 120 -Method Post -Headers $headers -Uri "$api/$($r.repo)/releases/$($release.id)/assets?name=$([uri]::EscapeDataString($leaf))" `
            -Form @{ attachment = Get-Item -LiteralPath $a } | Out-Null
    }
    # Anonymous readback of every asset.
    $check = Invoke-RestMethod -TimeoutSec 20 "$api/$($r.repo)/releases/tags/$($r.tag)"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) "release-check-$($r.repo)"
    New-Item -ItemType Directory -Force $tmp | Out-Null
    $results = foreach ($a in $r.assets) {
        $leaf = Split-Path $a -Leaf
        $remote = $check.assets | Where-Object name -eq $leaf
        if (-not $remote) { "$leaf=missing"; continue }
        Invoke-WebRequest -TimeoutSec 120 -Uri $remote.browser_download_url -OutFile "$tmp/$leaf" | Out-Null
        $same = (Get-FileHash -Algorithm SHA256 "$tmp/$leaf").Hash -eq (Get-FileHash -Algorithm SHA256 -LiteralPath $a).Hash
        "$leaf=$(if ($same) { 'ok' } else { 'MISMATCH' })"
    }
    Remove-Item -Recurse -Force $tmp
    "$($r.repo) $($r.tag) release id=$($release.id) prerelease=$($check.prerelease) url=$($check.html_url) assets: $($results -join ' ')"
}
