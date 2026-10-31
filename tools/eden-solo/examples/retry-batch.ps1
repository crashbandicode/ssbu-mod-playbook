# Example from ssbu-gnw-judge-log (eden/run_free_batch.ps1, 2026-10-30): runs
# one match after another until $Wanted runs succeeded or $MaxRuns were
# tried. The runner it calls (eden/run_free_match.ps1 in that repo) exits 2
# when the plugin's own log shows no `gamewatch session=` line within 30 s
# of the match starting, so a wrong fighter pick costs 30 s and the run is
# retried under the next label. Paths and the runner's parameters are that
# repo's; the loop is the part to copy.
param(
    [string]$NroPath = 'eden/nro/libssbu_gnw_judge_log.diag.nro',
    [int]$FirstLabel = 31,
    [int]$Wanted = 10,
    [int]$MaxRuns = 14,
    # Final Destination only until the stage pointer is steered like the
    # fighter pointer (bf/sbf taps are unverified).
    [string[]]$Stages = @('fd'),
    [string]$Display = ':63'
)
# Runs free-play matches one after another until $Wanted succeed (exit 0)
# or $MaxRuns were tried. A run that never saw Game & Watch (exit 2) or
# failed is retried under the next label.
$ErrorActionPreference = 'Continue'
Set-Location (Join-Path $PSScriptRoot '..')
$done = 0
$label = $FirstLabel
$tried = 0
while ($done -lt $Wanted -and $tried -lt $MaxRuns) {
    $name = 'g{0:D2}' -f $label
    $stage = $Stages[$tried % $Stages.Count]
    $stamp = [DateTimeOffset]::UtcNow.ToString('HH:mm:ss')
    Write-Output "$stamp start $name stage=$stage seed=$label"
    & ./eden/run_free_match.ps1 -NroPath $NroPath -RunLabel $name -Display $Display -Stage $stage -Seed $label *> "eden/$name-runner.log"
    $code = $LASTEXITCODE
    $stamp = [DateTimeOffset]::UtcNow.ToString('HH:mm:ss')
    if ($code -eq 0) { $done++ }
    Write-Output "$stamp end $name exit=$code done=$done"
    $label++
    $tried++
    Start-Sleep -Seconds 20
}
Write-Output "batch finished: $done successful runs in $tried tries"
