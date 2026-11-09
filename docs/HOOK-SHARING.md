# One hook owner and callback registration

When plugins use the same event, one provider owns the patch and exports
a callback-registration function; consumers register instead of installing
another patch. The owner's hook runs its own logic, then every registered callback.
Refusing merely because another plugin has hooked a site leaves one plugin
disabled and does not satisfy coexistence.

Dedicated providers have their own ABIs, such as `add_nro_load_hook`, param
hooks, ARCropolis callbacks or exported Online Deluxe state. A consumer needs
the provider installed or an explicitly designed fallback. These exports
are not interchangeable with inline-hook sharing v1. Dated provider/source
examples are in [incident history](WORKFLOW.md).

For peer plugins sharing main-image offset `X`, inline sharing v1 is:

1. Each exports `ssbu_inline_site_<x>_v1()` using lower-case hex, for example
   `ssbu_inline_site_1a1c118_v1`. It returns `SiteShareV1` with
   `abi_version`, `offset`, and `register(callback)`.
2. In `main`, look up that name through `nn::ro::LookupSymbol`. Skyline
   loads every plugin before running any `main`, so all see the first loaded
   exporter for that load order.
3. The plugin whose own table is returned is owner: read/validate the site,
   install the one hook, chaining over a valid foreign hook if present, and
   call registered callbacks with `*mut InlineCtx` using Skyline 0.6 layout.
4. A guest registers its callback and installs no patch at that site.

Run this election before the site's byte guard. A guest may arrive after
the owner's patch and would reject that patch if it guarded first. The
guest still guards every other site it calls or relies on; `register`
guards the shared site and refuses only when the owner could not hook it.

Registration must work both before and after the owner's `main`:

- Before `main`, check the version and read the original site, accepting
  the stock or valid foreign forms under [HOOK-CHAIN](HOOK-CHAIN.md).
- After `main`, answer from stored per-site state (hooked versus not
  hooked), rather than re-reading the owner's own patched bytes. Keep the
  equivalent of a `HOOKED` flag; a fresh guard would reject later guests.

Keep ownership separate from the owner's own feature. If an unavailable
called/relied site disables that feature, an owner still installs a usable
shared hook for its guests, omits its own logic and logs
`label=off site=<name> … result=hook_only`. In that state as a guest it
registers nothing and logs `result=not_installed`.

The install line records `role=owner … guests=<n>` or
`role=guest owner=<address>` for each site. `guests` counts only guests that
registered before the owner's `main`; later registrations do not make the
boot count a total. A multi-site plugin logs each site's role separately.

Prove both load orders and the [foreign stand-ins](HOOK-CHAIN.md) in Eden.
Keep callback activity, owner/guest and hook-only outcomes per site; historical
passing examples do not qualify a changed implementation. Reusable source
examples are gsp-visualizer/ssbu-quiet-elite-switch `src/share.rs` and
elite-rematch `src/share.rs`, with their `test-plugins/share-probe` plugins.

## Transitional baseline reference

Current concern policy is above; baseline detail remains during the migration for linked concerns and evidence extraction. The operator-approved task reading and screenshot policy take precedence.

# Sharing a hook between Skyline plugins

2026-10-30. For agents working on the operator's Smash Ultimate (13.0.5)
Skyline plugins. When the operator says a plugin should **"reuse"** a hook,
**"use it instead"**, or that **"both plugins should still work if they
clash"**, this is what they mean.

## The problem

Two plugins that hook the same instruction collide. The second Skyline hook
relocates whatever the first one wrote there: a one-word branch relocates
cleanly, but a far jump's 8-byte address is decoded as instructions
(deluxe-rewrite's 2026-10-16 boot crash), and a far jump written over a
one-word hook covers the word that hook returns to. Refusing to install when
the bytes are already changed avoids the crash, but then the second plugin
silently does nothing. That is not acceptable.

## The pattern: one owner, everyone else registers

One plugin owns the hook and exports a **registration function**. Every other
plugin that needs the same event calls that function with a callback instead
of hooking. The owner's hook calls every registered callback. One patch on
the instruction, every plugin works.

Ask an agent for it as: *"Don't hook X yourself if another plugin provides
it; register a callback with the plugin that owns the hook (a hook-provider /
callback-registration API, found by its exported symbol), so both plugins
work."*

### Examples already in use

| Owner (provider) | Export | Used by |
| --- | --- | --- |
| `libnro_hook.nro` | `add_nro_load_hook` (Skyline's `nro::add_hook`) | Training Modpack fork (`src/lib.rs`) |
| `libparam_hook.nro` | param load hook (`add_hook(params_main)`) | Training Modpack fork (`src/training/mod.rs`) |
| ARCropolis | `arcrop_register_callback`, `arcrop_load_file` | ssbu-fx-control (`src/runtime/arc.rs`) |
| Online Deluxe | `ssbu_online_deluxe_online_context_v1` (exported state instead of hooks) | ssbu-fx-control (`src/runtime/online.rs`) |

These are dedicated providers: the consumer needs the provider installed
(the Training Modpack fork panics without them) or a fallback (ssbu-fx-control
installs two hooks of its own when Deluxe is absent).

### Without a separate provider: inline-hook sharing v1

gsp-visualizer, ssbu-quiet-elite-switch and elite-rematch use `src/share.rs`
(the same file in all three), where **any** plugin that wants a site can be its
owner:

1. A plugin that wants main-image offset `X` exports
   `ssbu_inline_site_<x>_v1()` (lower-case hex, e.g.
   `ssbu_inline_site_1a1c118_v1`). It returns a `SiteShareV1` table:
   `abi_version`, `offset`, `register(callback)`.
2. In `main` it looks that name up with `nn::ro::LookupSymbol`. Skyline loads
   every plugin before any `main` runs, so every plugin gets the same answer
   (the first loaded exporter) in any load order.
3. **Owner** (the table returned is its own): reads the site's bytes
   (`chain::entry`, below), installs the one hook, on another plugin's hook
   there if it finds one, and from it runs its own logic and then every
   registered callback (`*mut InlineCtx`, Skyline 0.6 layout).
   **Guest** (another table came back): registers its callback and installs
   nothing.
4. The install line says which: `role=owner … guests=<n>` or
   `role=guest owner=<address>`. `guests` counts only guests that loaded
   earlier; later ones register after the owner's `main`. Plugins with several
   sites write it per site (elite-rematch: `popup_parsed=guest`).

**Pitfall (found in Eden):** run the election **before** the byte guard. A
guest finds the site already changed by the owner's hook; guarding first
makes it refuse, which is exactly the failure this pattern exists to avoid.
The guest still guards everything else it relies on; the owner's `register`
guards the shared site itself (and refuses only if the owner would not hook
either).

**The owner's side of it:** `register` can be called before the owner's
`main` (a guest that loaded earlier) or after it. Before, check the version
and read the site (it still holds the 13.0.5 words or another plugin's hook,
which `chain::entry` accepts). After, answer from the per-site state `main`
stored (1 hooked, 2 not: gsp-visualizer `APPLY_STATE`, ssbu-quiet-elite-switch
`POPUP_STATE`, elite-rematch `STATE`), not by reading the site again: it now
holds the owner's own hook, so a byte check there refuses every later guest
(both share probes keep a `HOOKED` flag for this; gsp-visualizer's re-read the
site until 2026-10-31, and in Eden e17, loaded first as the owner, it then
took gsp-visualizer as its guest). The owner hooks the shared site for its guests even when its
own feature cannot run: gsp-visualizer 0.3.0 with a relied-on or called site
it cannot use still installs the hook, leaves its label out of it and logs
`label=off site=<name> … result=hook_only`; as a guest in that state it
registers nothing (`result=not_installed`).

### When the other plugin does not share

A plugin that hooks without sharing v1 still must not turn yours off. The
rules below are elite-rematch's (`model::entry`, `runtime::hook_inline`,
written into that plugin); gsp-visualizer 0.3.0 and ssbu-quiet-elite-switch
0.2.0 have them in one shared file, `src/chain.rs` (identical in both, like
`share.rs`), instead of installing nothing when the site is not vanilla.

- **Function entry** (replace hook): another plugin's far jump there
  (`ldr x17, #8; br x17; .quad handler`) means install with no trampoline
  and call `handler` as your original, so both run (deluxe-rewrite
  `src/hook_chain.rs` `plan_far_chain`; elite-rematch `hook_lookup`). Never
  ask Skyline to relocate that jump.
- **Inline site** (`chain::entry`, `chain::hook::inline`): the 64 bytes are
  read against the 13.0.5 ones before hooking, and the install line says
  `<site>=<entry>/<outcome>` (`apply=`, `popup=`, `popup_parsed=`; outcomes
  `near`, `far`, `skipped`, `restored`):
  - `stock`: the 13.0.5 instructions, other plugins' hooks allowed past the
    words this hook replaces: a plain inline hook.
  - `chain`: another plugin's far jump over the site, to a handler that runs
    the instructions the jump replaced. Skyline must not relocate the jump's
    address word as code, so the inline hook goes on a small stub (five
    `nop`s for Skyline to relocate, then `adrp`/`ldr`/`br x17` to that
    handler, as the jump did) and the site gets a far jump to the stub with
    no trampoline. Checked: the handler is mapped readable and executable
    (`svcQueryMemory`), the stub holds a hook, the site jumps to the stub.
  - `branch`: another plugin's one-word `b` over the first word. Skyline
    relocates it into this plugin's trampoline; that plugin's handler
    returns to the word after its branch, so this hook must come out as a
    one-word branch too with the words after it unchanged (a far jump would
    cover that return word). Checked after installing; otherwise the words
    are put back (`restored`).
  - `foreign`: anything else (a non-hook change in the replaced words, a far
    jump without its `nop` at an address 4 past an 8-byte boundary, a jump to
    nowhere or into itself): that one site is left alone (`skipped`), not the
    whole plugin.
- **Sites only called or relied on** (`chain::usable`; gsp-visualizer
  `Kind::Called`, `Kind::Relied`): a called function accepts other plugins'
  hooks anywhere in its 64 bytes, since calls go through them. A relied-on
  instruction keeps its own word: the hover call `bl .1a1c070` must stay, as
  the label checks the saved return address and a hook on the call would
  move it into the hooking plugin's trampoline; hooks after it are fine.
  gsp-visualizer logs `other_hook=<site>` for each such site carrying one.
- **Skyline's two forms:** a one-word `b`, or a far jump of 16 bytes, or 20
  (a leading `nop`) at an address 4 past an 8-byte boundary. In Eden
  (2026-10-30) inline hooks came out as one-word branches and a replace hook
  into a plugin as a far jump.

### Testing the chain in Eden

Diagnostic build (`--features diag`) with `foreign=inline` or `foreign=far`
in the plugin's `diag.txt` (`sd:/ultimate/<plugin>/diag.txt`). Before its own
hook the plugin then hooks its site as a plugin that does not share would
(`chain::hook::standin`): `inline` is a plain inline hook (a one-word branch
in Eden); `far` is a replace hook, so a far jump, to a stand-in handler that
counts the run and jumps on to Skyline's trampoline (the replaced
instructions, then back to the site). gsp-visualizer also puts an inline
stand-in inside the pane text setter it calls (`.37a2288`), for the
called-function rule. The install line shows `foreign=<mode>` and
`<site>=<entry>/<outcome>`; the stand-ins' run counts are logged as they
change (`diag standin apply_inline=<n> set_text_inline=<n> apply_far=<n>`,
`diag standin popup_inline=<n> popup_far=<n>`) next to the plugin's own
events, and both must keep counting. elite-rematch's `foreign=1` is the same
test: inline stand-ins on its three sites, a replace hook on the lookup.

**Tested.** elite-rematch, Eden 2026-10-30 (`docs/SAFETY.md`): `branch/near`
on its three inline sites, `chain/far` on the lookup. For `src/chain.rs`
(gsp-visualizer 0.3.0, ssbu-quiet-elite-switch 0.2.0):

Eden (2026-10-30/31), diagnostic builds of both with elite-rematch 0.3.1 and
gsp-visualizer's share probe, Elite Sephiroth seeded:

- `foreign=inline` (e15): `apply=branch/near … label=on other_hook=set_text
  result=installed heap_allocations=0` and `popup=branch/near
  result=installed`. The label showed; the stand-ins ran (`apply_inline=10`,
  `set_text_inline=2218`, `popup_inline=1`); switching to a fighter not in
  Elite Smash showed no pop-up (`suppressed total=1`); the probe's callback
  still ran from gsp-visualizer's hook.
- `foreign=far` (e16): `apply=chain/far` and `popup=chain/far`, the same
  results (`apply_far=10`, `set_text_inline=2206`, `popup_far=1`,
  `suppressed total=1`, probe hits 10).

## Guards (what "the same guards" means in these plugins)

- **Version:** install nothing unless the game is 13.0.5.
- **The hooked site:** its 64 bytes must be the 13.0.5 ones or other
  plugins' hooks, handled as above (sharing, or chaining); anything else
  leaves that one site alone (`<site>=foreign/skipped`): gsp-visualizer and
  ssbu-quiet-elite-switch then log `result=not_installed`, elite-rematch
  goes on with its other sites.
- **Relied-on instructions and called functions:** the 13.0.5 bytes or other
  plugins' hooks, as above: a relied-on instruction's own word must stay, a
  called function may be hooked anywhere; anything that is not a hook is
  not. gsp-visualizer then keeps the shared hook for its guests and turns
  only its label off (`result=hook_only`).
- Prefer **calling** a shared game function over **hooking** it (gsp-visualizer
  0.2 calls the pane text setter instead of hooking it), and hook the most
  specific instruction available.

## Where

- gsp-visualizer 0.3.0: `src/share.rs`, `src/chain.rs`, `src/model.rs`
  (`Kind`, `Site::usable`), `src/runtime.rs` (`main`, `register_apply`,
  `hook_apply`, `apply`), `docs/CSS-LABEL.md`, test plugin
  `test-plugins/share-probe`
  (https://git.syntaxbyexample.com/intpa/gsp-visualizer).
- ssbu-quiet-elite-switch 0.2.0: `src/share.rs` and `src/chain.rs` (the same
  two files as gsp-visualizer's), `src/runtime.rs` (`main`, `register_popup`,
  `popup_usable`, `hook_popup`), `docs/HOOK.md`.
- elite-rematch: `src/share.rs` for its three inline sites (`.147fb64`,
  `.147f4d0`, `.168a5b8`), chaining onto non-sharing hooks in
  `src/model.rs` (`entry`) and `src/runtime.rs` (`hook_inline`,
  `hook_lookup`); test plugin `test-plugins/share-probe` for `.147fb64`; Eden
  evidence in `docs/SAFETY.md` (https://git.syntaxbyexample.com/intpa/elite-rematch).
