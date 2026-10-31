# Checks remove-mtp.py compiles, builds the removal folder with its approval
# and copies both to the Surface.
$ErrorActionPreference = 'Stop'
$script = '/home/intpa/gsp-visualizer/eden/remove-mtp.py'
/usr/bin/python3 -I -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" $script
if ($LASTEXITCODE -ne 0) { throw 'remove-mtp.py does not parse' }
$out = '/home/intpa/gsp-visualizer-work/switch-mtp-remove-shaderdb'
if (Test-Path $out) { throw "exists: $out" }
New-Item -ItemType Directory "$out/shaderdb" | Out-Null
Copy-Item $script "$out/remove-mtp.py"
$approval = [ordered]@{
    operator_order = '2026-10-31 operator: remove shaderdb-probe from the card (a leftover diagnostic the operator does not use).'
    operator_removal_authorized = $true
    name = 'libssbu_shaderdb_probe.nro'
    data = 'ultimate/shaderdb-probe'
    old = '01b8897ffaddfc43a8455a41080c299b6e6d41f9f151a284726c2072cc95185a'
}
[IO.File]::WriteAllText("$out/shaderdb/operator-approval.json", ($approval | ConvertTo-Json) + "`n", [Text.UTF8Encoding]::new($false))
scp -q -r -o BatchMode=yes -o ConnectTimeout=10 $out intpa@dreamland-surface.lan:/home/intpa/gsp-visualizer-install/
"parse ok; scp exit=$LASTEXITCODE"
