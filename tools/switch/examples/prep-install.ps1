# Builds one install folder per plugin (NRO + operator-approval.json for
# upgrade2-mtp.py), checks each NRO hash, and copies the set to the Surface.
$ErrorActionPreference = 'Stop'
$w = '/home/intpa/gsp-visualizer-work'
$out = "$w/switch-mtp-vcut-1031"
if (Test-Path $out) { throw "exists: $out" }
$order = '2026-10-31 operator: cut a new minor version of each changed plugin that is on the Switch, update its NRO on the card, and sanity-test a match (Eden e18 passed).'
$items = @(
    @{ dir = 'elite-rematch'; name = 'libelite_rematch.nro'; data = 'ultimate/elite-rematch'
       old = '689c423fbde7e60e8ce1de93bb3cb622192b51d8d23baa60a036b671efec1051'; new = '4f13fd146a0b87a288df7a63823966a49d1f08afa6d69fd6a37dbe0bf62618db' },
    @{ dir = 'desync'; name = 'libssbu_desync_detector.nro'; data = 'ultimate/ssbu-desync-detector'
       old = '95e7def98090b4368851182e5952306bd26105fd8d6d4f57da65ecece61e1db2'; new = '29067bb63fd2721ed577f613088614a365804c9ac7101c4ad4ff8b8df3cd23a8' },
    @{ dir = 'deluxe'; name = 'libssbu_online_deluxe_rewrite.nro'; data = 'ultimate/ssbu_online_deluxe_rewrite'
       old = 'a0a4c9a04409bea8a6225995c9c31ccbd9f275a3a8e991e22e3357a37406b79b'; new = 'a068094b40f2a58423b3cc0954332014e6901475afbe8918aa1656d415dc0ca4' },
    @{ dir = 'refresh-clock'; name = 'libssbu_refresh_clock.nro'; data = 'ultimate/ssbu-refresh-clock'
       old = '80bab7c8cc4a4dfc1200dda9852c84df1ac3cef71c9af4617774396844853a20'; new = '1246f448fcd42092423ff9df51b7eaade806f95ec3da460ba51cf8f2576ff3c0' },
    @{ dir = 'shaderdb'; name = 'libssbu_shaderdb_probe.nro'; data = 'ultimate/shaderdb-probe'
       old = '01b8897ffaddfc43a8455a41080c299b6e6d41f9f151a284726c2072cc95185a'; new = '9cf97796dab55d38d472e6de05f4385d5e8dfe6139c75cd0d35b86f21680502e' }
)
New-Item -ItemType Directory $out | Out-Null
Copy-Item /home/intpa/gsp-visualizer/eden/upgrade2-mtp.py "$out/upgrade2-mtp.py"
foreach ($i in $items) {
    $d = "$out/$($i.dir)"
    New-Item -ItemType Directory $d | Out-Null
    Copy-Item "$w/vcut-1031/$($i.name)" "$d/$($i.name)"
    $h = (Get-FileHash -Algorithm SHA256 "$d/$($i.name)").Hash.ToLower()
    if ($h -ne $i.new) { throw "$($i.name): $h is not the tested build" }
    $approval = [ordered]@{ operator_order = $order; operator_install_authorized = $true; name = $i.name; data = $i.data; old = $i.old; new = $i.new }
    [IO.File]::WriteAllText("$d/operator-approval.json", ($approval | ConvertTo-Json) + "`n", [Text.UTF8Encoding]::new($false))
    "$($i.dir): $($i.name) $($h.Substring(0, 8)) replaces $($i.old.Substring(0, 8))"
}
scp -q -r -o BatchMode=yes -o ConnectTimeout=10 $out intpa@dreamland-surface.lan:/home/intpa/gsp-visualizer-install/
"scp exit=$LASTEXITCODE"
