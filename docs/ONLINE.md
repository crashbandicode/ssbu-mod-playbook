# Online plugin acceptance

Apply these conditional rules when the plugin runs during online play.
Review ssbu-desync-detector's `docs/WHAT-IS-CHECKED.md` **hook by hook**:
draw no checked random number, skip/reorder no native code that draws one,
and write no simulated state. Record that code review in the evidence doc.
Passing runtime checks does not replace this review.

Run the **same replay with and without the plugin**, with the desync detector.
Its checked values **and positions** must match; the detector's comparison
example is `cargo run --example compare`. Replay tests cover in-match paths;
they cannot exercise online-only results/pop-up/rematch/matchmaking behavior.

For an online-only path, use two consoles with the detector on both and the
plugin on only one. Play the changed path all the way through, then check
the **session after it** too. Every session on both consoles ends with
`sync_end … desync_seen=0` at the **same `frame=`**. Retain both detector logs,
the asymmetric plugin pins, run/build identities and changed-path evidence.
Seed server/rating state where necessary so the game naturally takes the
path, instead of forcing it with a diag build.

Use current lifecycle pair config and runner project/UI steps. Same-host
pairs require explicit `pair_authorized:true`, distinct profiles/inputs and
normal [HOSTS](HOSTS.md) admission. Pair frontend room setup happens **before
game load**: lifecycle `defer_load`, runner room UI, then bounded terminal
`game.load` and fresh-frame status. TLS/rating runtime isolate intended
peers; preserve seed choices and settings with the run. The lifecycle does
not itself implement LDN frontend room setup or implicit socket forwarding.

A pair can share one authorized PC or use Surface/Yoga executors, with
executor-local runner/control paths. Ally hosting/pairs need separate grant
and firewall policy; the inbound Block lifecycle proof does not qualify them.
[WINDOWS](WINDOWS.md) lists demonstrated and unqualified paths. Old
`local_pair.ps1`/`pair.ps1` recipes remain dated history, not evidence of modern
admission/restoration. Finish with [LIFECYCLE](LIFECYCLE.md) Down and health review.

Historical replay counts, rematch sequences and concrete pair routes are in
[route history](MACROS.md); none replaces new
matching-frame receipts for the changed paths.
