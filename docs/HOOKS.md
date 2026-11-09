# Choose and guard hook sites

Finding offsets is reverse engineering; use that workflow before this one.
Before choosing a site, scan a dated copy of the card's NROs with
`tools/build/scan-offsets.py <nro-folder> <hex-offset>…`. Scan the site and,
for a virtual function, its vtable slot. Keep the scan output and copy date;
use fresh local copies from a card preflight when the set has changed.

The scanner is advisory. It recognizes exact 32/64-bit constants and a
low-half-first MOVZ/MOVK pair within eight instructions; its nearby range
applies to that pair recognition. It does not find every computed offset,
MOVZ/ORR-free form or plugin added after the snapshot. A quiet scan is not
runtime safety proof. [HOOK-SHARING](HOOK-SHARING.md) and
[HOOK-CHAIN](HOOK-CHAIN.md) govern sharing, foreign hooks and load orders.

Install nothing unless the game reads `13.0.5`; otherwise log
`result=unsupported_version`. Guard 64 bytes at every hooked site, every
called function and every relied-on instruction against the 13.0.5 table.
A mismatch leaves that site alone and names it in the install log. The
guard accepts recognized other-plugin hooks only under the category-specific
chain rules; it is not an unconditional vanilla-byte equality rule.

- At the hooked site, accept stock instructions or a valid shared/chained
  hook; reject unrelated changes as `<site>=foreign/skipped`.
- A called function may contain valid hooks anywhere in its guarded bytes,
  because calls go through them. Log `other_hook=<site>` when present.
- A relied-on instruction keeps its own word; valid hooks after it may be
  accepted. For example, moving a relied-on call into a trampoline changes
  its saved return address. Reject non-hook modifications in either category.

Elect a sharing owner before guarding the shared site; guests still guard
everything else they rely on. Registration and hook-only ownership are
specified in [HOOK-SHARING](HOOK-SHARING.md).

Prefer calling the game function over hooking it. When hooking, choose the
most specific instruction and place the hook after the prologue, leaving a
function entry available to replacement hooks. Hooks also follow the
no-allocation and no-wait rules in [BUILD](BUILD.md).

When another plugin hooks or may hook a site, prove sharing/chaining in
Eden with both `foreign=inline` and `foreign=far`, and both load orders.
N/A is allowed only when the cited scan found nobody at the site. Retain
per-site install outcomes and the stand-in/plugin event counters, not just
a successful boot. The checker recognizes some guard shapes and sizes;
it does not enumerate every hook, call or relied-on instruction for you.
