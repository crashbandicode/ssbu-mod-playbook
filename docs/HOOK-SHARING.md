# One hook owner and callback registration

When the operator says “reuse” a hook, “use it instead”, or “both plugins
should still work if they clash”, this is the required pattern.
When plugins use the same event, one provider owns the patch and exports
a callback-registration function; consumers register instead of installing
another patch. The owner's hook runs its own logic, then every registered callback.
Refusing merely because another plugin has hooked a site leaves one plugin
disabled and does not satisfy coexistence.

Dedicated providers have their own ABIs, such as `add_nro_load_hook`, param
hooks, ARCropolis callbacks or exported Online Deluxe state. A consumer needs
the provider installed or an explicitly designed fallback. These exports
are not interchangeable with inline-hook sharing v1. Dated provider/source
examples are in [incident history](../history/PLUGIN-AND-HOST-INCIDENTS-2026-10--11.md).

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
