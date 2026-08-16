# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

A single owner-operator, at their desk, with the companion running full-screen
on a dedicated iPhone 6s beside the computer. One trusted user — not a
household device and not multi-tenant. Confirmed consequences: the single
shared `COMPANION_TOKEN` is a sufficient auth model, and memory, facts and
conversation history need no per-user scoping or privacy partitioning.

The same person uses the desktop dashboard, in a different posture: at the
computer, administering the thing rather than talking to it.

## Product Purpose

A local AI desk companion. It holds a conversation, remembers durable facts,
fires reminders, listens and speaks, controls the computer it lives beside,
and switches the lights — all from hardware in the room.

Success is two things at once, and the order matters: it has to work every day
on the desk, and it has to hold up when shown to someone. Reliability is the
floor. Expression is earned on top of it and never at its expense.

## Positioning

The mechanism a neighbouring product could not truthfully copy is that nothing
leaves the LAN. The model (Ollama), speech-to-text (faster-whisper), speech
synthesis (kokoro), memory, and every device it controls are all on hardware
the owner owns. A cloud assistant cannot make that claim, and a cloud
assistant is what it is being measured against.

The second is embodiment: a dedicated always-on screen with a face, plus a
hardware wake trigger, rather than an app you open.

## Operating Context

- The phone sits in a stand on the desk, screen on, as an installed PWA in
  standalone mode. It is glanced at, spoken to, and occasionally typed on.
- The backend runs on the Windows PC in the same room, serving both the PWA
  and the dashboard over the LAN.
- HTTPS with a locally generated CA is a functional requirement, not a
  security nicety: `getUserMedia` (tap-to-talk) and the service worker only
  run in a secure context, and iOS blocks an HTTPS page from calling an HTTP
  backend.
- An ESP32-S3 keyword spotter can trigger the companion over the network
  without touching the phone.
- The dashboard is opened on the desktop to inspect and administer.

## Capabilities and Constraints

Confirmed capabilities: chat against a local model with native tool-calling;
durable memory; reminders including recurring ones; tap-to-talk transcription
and spoken replies; PC control (media transport, volume, lock, app launch,
now-playing, system stats, opening web pages); smart-home switching; a live
event stream pushed to the phone; first-run pairing by QR.

Binding constraints, all confirmed by the user:

- **Fully local.** No cloud LLM, no telemetry, no third-party service
  dependency. Future work must not introduce one.
- **iPhone 6s / iOS 12.** The fixed 375×667 stage stays, as do the iOS 12
  accommodations already in the code (no flex `gap`, no `AbortController`,
  16px minimum font size on inputs to prevent focus zoom). This rules out
  modern-only CSS and most animation libraries.
- **The face is the identity.** The animated glowing face is the product's
  signature and must survive any redesign. This is why the phone shell kept
  the face as the screen rather than demoting it to a header mark.
- **The dashboard is admin-only.** A control surface for the owner, not a
  surface that has to persuade anyone.

Technical constraints inherited from the environment: PC control is
Windows-only; there is no Docker or WSL on the host, which is why smart-home
control talks to TP-Link devices directly over the LAN rather than through
Home Assistant.

Undecided, deliberately: whether the ESP32 trigger becomes a true wake-word
spotter (it is currently an adaptive energy threshold, a documented tradeoff).

## Brand Commitments

- Name: **Companion**.
- The face — two eyes and a mouth arc, six emotion states, an idle blink loop
  — is the identity.
- Cyan `#4dd8e6` on near-black `#05070a` is the established palette across
  both surfaces.
- Typography and token architecture come from **Nocturne**, a design system
  the owner maintains in Claude Design and imports via DesignSync. Its palette
  is deliberately not used; its type and structure are.

## Evidence on Hand

Real, in-repo: a working backend, a working PWA, a working dashboard, ESP32
firmware, and a full redesign mockup set ("Companion Redesign.dc.html") in the
owner's Claude Design project which both surfaces were built from.

There are no users besides the owner, no testimonials, no benchmarks, no
pricing and no deployment story. Future work must not invent any.

## Product Principles

1. **Reliability is the floor; expression is earned on top of it.** It is a
   daily driver first and a showpiece second, in that order.
2. **Nothing leaves the LAN.** Any feature that needs a cloud service is the
   wrong feature.
3. **The face is not decoration.** It is the product's identity and the thing
   a redesign must protect.
4. **The oldest supported device sets the ceiling.** A technique the iPhone 6s
   cannot run is not available, however good it looks elsewhere.
5. **Two surfaces, two jobs.** The phone is the product and is glanced at from
   a distance; the dashboard is the control room and is read up close. They
   share a palette, not a layout language.

## Accessibility & Inclusion

No formal standard was established as a requirement. One product-specific need
is confirmed by the operating context and already honoured in code:
`prefers-reduced-motion` disables the idle loops and entrance animations,
because this is an always-on screen in peripheral vision.
