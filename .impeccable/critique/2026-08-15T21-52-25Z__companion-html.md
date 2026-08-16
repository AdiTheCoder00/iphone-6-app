---
target: companion.html
total_score: 22
max_score: 40
na_heuristics: 
p0_count: 2
p1_count: 2
timestamp: 2026-08-15T21-52-25Z
slug: companion-html
---
Method: dual-agent (A: a6c0764eb1dadf6f5 · B: a2b868a382631e301)

## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|-----------|-------|-----------|
| 1 | Visibility of System Status | 2 | Deck asserts "Nothing playing" / "Nothing scheduled" / "—" when it cannot reach the PC at all |
| 2 | Match System / Real World | 3 | Copy is genuinely human; docked for unexplained "Access token" and "Remembers" as a noun |
| 3 | User Control and Freedom | 1 | No Escape handler anywhere in 4,485 lines; pairing has no back, no skip, and is a hard gate |
| 4 | Consistency and Standards | 2 | Token is type=password in settings, type=text in pairing; three accents render at once in think |
| 5 | Error Prevention | 3 | Strong: QR never auto-pairs, safeBackendUrl validates, all requests timeout-wrapped |
| 6 | Recognition Rather Than Recall | 2 | Settings is 12 ungrouped fields; long-press disclosed only inside the sheet it opens |
| 7 | Flexibility and Efficiency | 2 | No keyboard path; deck page 2 is swipe-only; scan box not focusable |
| 8 | Aesthetic and Minimalist Design | 3 | Idle screen is genuinely restrained; docked for 47% empty deck in its failure state |
| 9 | Error Recovery | 2 | checkHealth distinguishes four states well, but retry strip collides with bubble |
| 10 | Help and Documentation | 2 | Good hint text, but 11px at 3.08:1 at the bottom of a 967px scroll |
| **Total** | | **22/40** | **Acceptable — significant improvements needed** |

## Design Specificity Verdict

**Split verdict: the face is exceptionally specific; the deck bolted onto it is generic — and the seam is where nearly every defect lives.**

LLM assessment: the face is the layout's organizing principle, not a mascot. `.face-root { padding-bottom: var(--deck-h) }` re-centres the eyes above the sheet rather than the screen. Every animated element owns exactly one source of transform, which is why six emotions, a blink loop, breathing and lip-sync coexist without stomping each other. `--talk-open` is added to `--mouth-h` so speech does not erase the emotion's mouth. The glow is a radial gradient rather than a blur filter, explicitly costed for a 2015 A9. None of this transfers to another product.

The deck is the opposite: a rounded bottom sheet with a grab handle, paged carousel, page dots and a scrolling pill row could be lifted into any music or IoT app unchanged. Section 6 of the stylesheet carefully retargeted `.retry` and `.bubble` for the deck's arrival and forgot `.think-dots`, `.think-caption` and `.dnd-note` — which is exactly where the two P0s come from.

Deterministic scan: 5 findings, all warnings, exit code 2 — 3x layout-transition (L211 margin, L349 height, L1082 width), 1x dark-glow (L235), 1x radial-halo (L105). All five verified to exist at the reported lines. The detector ran DEGRADED (HTML parser modules unavailable, regex fallback); custom properties, selector matching and computed contrast were NOT evaluated, so findings are an undercount. That undercount is proven: the detector missed a 1.07:1 contrast failure on the deck's primary text.

The two "slop" findings are sanctioned exceptions, not defects: the glow and halo are this product's pinned identity per PRODUCT.md. Assessment B added measured context the degraded detector could not see — `.ambient__blob` composites its halo at `opacity: 0.07`.

Visual overlays: no user-visible overlay was presented. Mutation capability was confirmed (title set, script appended and executed), but the live-server overlay flow was not run; evidence came from CLI scan plus JavaScript measurement instead.

## Overall Impression

The face is some of the most disciplined interface engineering in this codebase, and the deck layered on top of it is where the craft stops. Three of this session's newest treatments — the deck title, the thinking indicator, the DND chip — shipped either invisible, occluded, or on top of other text. The single biggest opportunity is not visual: it is that the 375x667 stage is now composed by hand-tuned absolute offsets that do not know about each other, and it needs a layout that does.

## What's Working

1. The face is a transform-discipline system, not an animation. One transform source per layer; custom properties transitioned via the properties that consume them. This is precisely why the emotion engine survived two new states this session without regression.
2. Failure copy is honest, specific, and paired with the right action. `checkHealth()` distinguishes unreachable / warming / model-unavailable / healthy and gives each different text and a different button.
3. Consent and untrusted-input handling are built at the point of decision. The mic card makes the local-only claim where the user is deciding; the QR decoder refuses to auto-pair because the code sets the server every later request goes to.

## Priority Issues

**[P0] The deck's primary text renders black on near-black (1.07:1)**
`.deck__title` declares font-size, weight and ellipsis handling but no `color`, and nothing in the ancestor chain sets one — `html, body` sets background but never color. Computed: `rgb(0,0,0)` on `rgb(7,12,16)`. Independently confirmed. This hits `#deckPcTitle` (the track playing) and `#deckNextText` (the next reminder) — the two strings the deck exists to show. Fix: `.deck__title { color: #dfe9ee; }` and set a `color` on `html, body` so nothing else can silently inherit black. Command: `/impeccable clarify`

**[P0] The thinking indicator has never been visible**
`.think-dots` and `.think-caption` are positioned `top: calc(50% + 84px/108px)` against `.face-root`, which spans the full 667px stage — landing them at y736/y760 while `.deck` starts at y687 with a higher z-index. `elementFromPoint` at the dots' centre returns `deck__title`. A local model takes seconds to first token; this is the affordance that stops the owner tapping send twice. Fix: anchor to the gap the deck left, as `.retry` already was — `bottom: calc(var(--deck-h) + 34px)` / `+ 14px`. Command: `/impeccable layout`

**[P1] Four absolute-positioned strips collide in one band**
`.dnd-chip` overlaps `.glance__meta`; `.dnd-note` overlaps `.retry`; `.bubble` overlaps `.retry` by 44px. All four are positioned off unrelated anchors. DND-on plus backend-down is the normal overnight state of a desk device whose PC is asleep, so this is the default for eight hours a day, not an edge case. Fix: make the band between mouth and deck a single flex column owning `.retry`, `.dnd-note` and `.bubble`, showing one at a time in priority order. Command: `/impeccable layout`

**[P1] Pairing is a modal with no exit, no focus, and an inaccessible primary action**
`#pair` is `aria-modal="true"` but focus never moves in (activeElement stays BODY), 19 background controls remain tabbable, there is no Escape handler anywhere in the file, no back on step 2, and no skip. `#pairScan` — the primary path — is a `<div>` with no role, tabindex or label. If the PC is not on yet, the owner's only escape is clearing site data. Fix: focus `#pairUrl` on show, `inert` the rest of the stage, convert the scan box to a labelled `<button>`, add back and a low-emphasis "Skip for now". Command: `/impeccable harden`

**[P2] Backend-down is styled as grief, and the palette fractures doing it**
`checkHealth()` sets `sad` for any unreachable backend; the preset tweens `--accent` on `.stage`, desaturating send button, meters and dock. `sad` is not in QUIET_EMOTIONS so the clock — the one readout needing no backend — also disappears. Meanwhile `.retry__connect` hardcodes `#53d5e5` and never joins the tween, so three accents render at once during think (eyes amber, send desaturated blue, Connect bright cyan) because think's colour is defined twice and differently (JS preset `[122,168,240]` vs CSS `#f0c26a`). Fix: use `sleepy` for unreachable and reserve `sad` for genuinely broken; add it to QUIET_EMOTIONS; drive `.retry__connect` from `var(--accent)`; delete one of the two think colours. Command: `/impeccable colorize`

## Persona Red Flags

**Jordan (first-timer)**: discovery times out after 4s and silently drops them onto step 2 with `#pairDots` already showing 2 of 3 lit for a step they never saw. `#pairToken` has label "Access token" and placeholder "Access token" — no hint where to find it, no indication it may legitimately be blank. The explanation exists only as a placeholder inside a settings sheet reachable by an undisclosed long-press. No skip.

**Sam (accessibility)**: no Escape handler in the entire file, so `#settings`, `#micPrompt` and `#pair` are all unclosable by keyboard. `#pairScan` is an unlabelled div. `.field__range` sets `outline: none` with no focus substitute. `.retry` is `role="status"` containing a button — violating the rule the file documents at line 1489. Deck page 2 (the next reminder) is reachable only by horizontal swipe. Three infinite animations (micPulse, retryPulse, thinkDot) escape the reduced-motion block, which PRODUCT.md names as the product's one confirmed accessibility need. No `<h1>`.

**Casey (one-handed, phone in a stand)**: `.deck::before` is a grab handle visually identical to the settings sheet's real one, but there is no drag/pointer/touch handler on `#deck` anywhere — a false affordance. `.dnd-chip` is 106x23, the documented one-tap way out of DND, well under the 44px every other control respects, and sits at y168 — the hardest reach on a stand-mounted phone.

## Minor Observations

- DND slashes a mic that still records: `#micBtn.disabled === false`; only the wake handler and `speak()` check the flag.
- `#pairToken` is `type="text"` while `#setToken` is `type="password"` — same secret, and the plaintext one is on a screen that never turns off.
- `loadDevices()`'s empty catch swallows backend-down, so the pill row renders silently empty, while the milder configured-but-empty case correctly says "Smart home not set up".
- `.settings__body` scrolls 967px in a 560px window across 12 fields with zero section headers, putting "Forget conversation" at the same visual weight as "Weather city".
- 44 distinct hex literals against 26 custom properties, only 2 of which are colour tokens; near-duplicate pairs present (#4dd8e6/#57d8e8, #7d8f99/#7d8f9a).
- `viewport-fit=cover` declared but no `env(safe-area-inset-*)` used — harmless on the 6s, latent on a notched device.

## Questions to Consider

1. The face has six emotions, but only `sad` is triggered by something the user did not cause, and it fires for the most routine event in the product's life — the PC being off. Is that a personality, or a health check with eyebrows?
2. The deck claimed 45% of a stage the face was designed to own, and in its failure state shows four dashes and 141px of nothing. Should `--deck-h` collapse to 0 until there is something real to show, letting the deck earn its space the way PRODUCT.md says expression must?
3. Two of the three treatments drawn for this redesign shipped invisible or overlapping. Is the real problem that new surfaces keep getting layered onto a fixed stage by hand-tuned absolute offsets, when what the stage needs is a layout that knows what else is on screen?
