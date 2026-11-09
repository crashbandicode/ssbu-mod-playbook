# Preserve foreign hooks at a shared site

Use [HOOK-SHARING](HOOK-SHARING.md) when a provider is available. A plugin
without sharing v1 must still coexist safely: do not disable your plugin
solely because it patched the site. Read the 64-byte guard before patching
and classify each site. [HOOKS](HOOKS.md) retains version and guard duties.

At a replacement function entry, a foreign far jump
`ldr x17, #8; br x17; .quad handler` means install without a trampoline and
call its handler as the original. Never ask Skyline to relocate the jump's
embedded address as instructions.

For an inline site, preserve these cases:

- `stock`: the replaced instructions are 13.0.5 stock; valid hooks beyond
  the words replaced are allowed. Install a plain inline hook.
- `chain`: a foreign far jump targets a handler that executes the replaced
  instructions. Put the inline hook on a stub of five `nop`s for Skyline
  to relocate, then `adrp`/`ldr`/`br x17` to that handler. Patch the site
  with a far jump to the stub, without a trampoline. Verify the foreign
  handler is mapped readable and executable with `svcQueryMemory`, the
  stub holds a hook, and the site jumps to it.
- `branch`: a foreign one-word `b` replaces the first word. Let Skyline
  relocate it into your trampoline. Your installed hook must also be one
  word, with every following word unchanged, because the foreign handler
  returns to the next word. Check after installation; restore the original
  words if it emitted a far jump or changed that return word.
- `foreign`: unrelated changes, an invalid/misaligned far form,
  nowhere/self targets, or unsupported entries leave that one site alone
  (`skipped`); do not turn off unrelated usable sites.

Skyline uses a one-word `b`, or a 16-byte far jump; at an address four
bytes past an eight-byte boundary the far form is 20 bytes with a leading
`nop`. A far form missing that required `nop` is foreign. Do not decode its
address as code or cover a branch handler's return word.

Called functions and relied-on instructions use different acceptance rules.
A called function accepts recognized hooks anywhere in its 64 bytes because
the call enters those hooks. A relied-on instruction keeps its own word;
hooks after it are allowed. The hover call `bl .1a1c070`, for example, must
stay when logic checks its saved return address. Log each accepted
`other_hook=<site>` and reject non-hook changes in either category.

Log `<site>=<entry>/<outcome>` per site, including `near`, `far`, `skipped`
or `restored`. Preserve [sharing](HOOK-SHARING.md)'s hook-only provider
behavior if a called/relied site disables only the owner's feature. Site
failures are not permission to install an unsafe patch elsewhere.

Exercise both diagnostic stand-ins, configured in
`sd:/ultimate/<plugin>/diag.txt` behind `--features diag`:

- `foreign=inline` installs a foreign plain inline hook before yours.
- `foreign=far` installs a foreign replacement far jump whose handler
  counts and jumps to Skyline's trampoline, then back to the original site.
- Include a called-function stand-in when the implementation uses that
  rule; gsp-visualizer puts one inside the pane text setter `.37a2288`.

The install line records `foreign=<mode>` and each entry/outcome. Stand-in
counts and the plugin's own events must both keep increasing. Retain those
logs and expected visual behavior under both plugin load orders; N/A needs
the card scan cited by [HOOKS](HOOKS.md). `foreign=1` in elite-rematch is
its historical equivalent: inline stand-ins on three sites and a replacement
hook on lookup. Source examples live in the plugins' `src/chain.rs`,
`model::entry`, `runtime::hook_inline`, `hook_lookup` and share probes;
dated results are in [incident history](WORKFLOW.md).
