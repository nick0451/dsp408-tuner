# The measurement interface

**Status: designed, not built.** Nothing here has been prototyped. The
Focusrite Scarlett Solo rig described in [hardware.md](hardware.md) remains the
working measurement rig and stays that way until this one is built *and*
validated against it.

> ### ⚠ Every component figure in this document is unverified
>
> Part numbers, prices, noise figures, channel counts and package types below
> are a **shortlist to check**, not a specification. CLAUDE.md forbids stating
> hardware figures from memory, and that rule does not stop applying because
> the hardware is ours. Nothing here is procurement-ready until a datasheet is
> cited beside it.

---

## Why build rather than buy

The tuner needs four microphones mounted once at ear height, so that a
measure → fit → apply → re-measure round runs without a human repositioning a
mic between sweeps. [hardware.md](hardware.md) establishes why four, and that
extra mics buy *unattended iteration*, not accuracy.

Surveying the market does not solve this, and the reason is worth stating
precisely rather than as "commercial gear is expensive". Multichannel
interfaces are large and costly because of requirements this project does not
have:

| Their requirement | Our need |
|---|---|
| 48 V phantom on every channel | None — measurement electrets need a few volts of bias |
| +4 dBu professional line stages | Line level into a car DSP's RCA inputs |
| Mains power | Host already has an automotive DC-DC |
| Studio-desk or rack form factor | Must work inside a vehicle |
| **A gain knob per channel** | **Actively harmful — see below** |

Strip those four and the remaining hardware is modest. Two of them — the
phantom supply and the gain controls — are also the ones that hurt us most
if retained.

There are two things a purpose-built box does that no purchased one does, and
both are correctness properties rather than conveniences.

### 1. Gain becomes a measured constant instead of a remembered one

`Provenance.gains_db` in [result.py](../src/tuner/measure/result.py) is part of
`comparable_to()`: two measurements taken at different preamp gain are refused
as incomparable, because a gain change between sessions masquerades as an
acoustic difference. That check is only as good as the number fed into it, and
on the current rig that number is *whatever a human typed after looking at a
knob*. The characterization in hardware.md records "input 2 gain at 7 o'clock",
which is an honest description of an unrepeatable setting.

There is no need for variable gain here. With a 24-bit converter, set each
channel's fixed gain so that full scale sits just above the capsule's acoustic
overload point. A 75–90 dB SPL measurement sweep then lands somewhere around
−45 dBFS, with the converter's noise floor far below the capsule's own
self-noise — so the gain buys nothing that the converter's dynamic range does
not already provide.

Fixing the gain deletes a subsystem (PGA or digital pot, plus its own
calibration), deletes a front-panel control, and turns `gains_db` into a board
constant that is true by construction.

**No gain controls anywhere on this device.**

### 2. The timing reference stops depending on operator discipline

The box generates the stimulus and captures it, so the loopback does not need
to be a cable. It can be a permanent internal trace — **tapped at the output
connector, after the output stage**, so the reference path includes everything
the DSP sees rather than bypassing the output amplifier.

`Measurement.has_timing_reference` then becomes true by construction. Under
the timing-reference rule in CLAUDE.md, that flag is the difference between a
measurement that can produce delay and phase and one that cannot; making it
structural rather than procedural removes the most consequential setup mistake
available in this project.

---

## What the UMIK-1 is for

**It is the transfer standard. It is not an array element, and four of them
would not be an array.**

This is already recorded at [hardware.md](hardware.md), and is restated here
because it is the question that recurs whenever the mic count comes up.

Four UMIK-1s are four independent crystal domains:

- **They drift against each other.** At typical crystal tolerance the relative
  slip across a single sweep is tens of samples. That is fatal to phase, and
  it smears the deconvolution enough to matter for magnitude.
- **None of them can carry a hardware loopback.** A UMIK has no output. Under
  the timing-reference rule every measurement from one is magnitude-only.
- **The host cannot open them as one stream.** Four USB audio devices require
  an aggregation layer, which resamples — the same resampling already measured
  on this project's rig as costing ~3 dB at 10–16 kHz.

So a four-UMIK array would deliver *faster acquisition of magnitude-only
data*: it pays the cost of multiple mics without buying the phase correctness
that makes them worth having, since a single mic with a loopback is already
phase-correct sequentially.

Its actual role is worth more than a fourth array slot. It is the factory-
calibrated reference that makes four cheap capsules trustworthy, through
`derive_calibration` in
[substitution.py](../src/tuner/cal/substitution.py). Mics calibrated against
the same reference by the same procedure share their common-mode error, so
their *relative* match — the thing spatial averaging and L/R asymmetry
analysis actually depend on — stays tight even though absolute accuracy is
only ±1.5–2 dB.

---

## Target specification

| | Value | Rationale |
|---|---|---|
| Analog inputs | **6** | 4 mic + 1 internal loopback + 1 external line/spare |
| Analog outputs | **4** | Drives all four DSP-408 analog inputs |
| Clock domains | **1** | The entire reason for building this |
| Sample rates | **48 / 96 kHz only** | See below |
| Resolution | 24-bit | |
| Input gain | **Fixed, calibrated, per channel** | No knobs, no PGA |
| Phantom power | **None** | ~5 V electret bias only |
| Mic cabling | **Balanced, 4-conductor + shield** | Non-optional in a 12 V vehicle |
| Power | **USB bus, ≤500 mA** | Host already has an automotive DC-DC |
| Connectors | USB-C device; 4× TA4F mic in; 4× RCA out | |

### Dropping the 44.1 kHz family

The DSP-408 runs at a fixed 48 kHz, and its I/O is analog, so our capture rate
is independent of it — which is why the current rig runs at 44.1 kHz, the
Scarlett's native rate.

When we choose the crystal, that freedom disappears and becomes an advantage.
A single 24.576 MHz oscillator covers 48 and 96 kHz with no PLL, no second
crystal, and no rate switching. Supporting 44.1 would add all three to serve a
rate nothing in this system needs.

### Balanced mic cabling is not optional

Five metres of unbalanced cable carrying a few millivolts, routed through a
vehicle alongside the alternator and ignition systems, is an antenna. The
balancing must happen **at the capsule**, not at the box — a differential
receiver at the far end of an unbalanced run corrects nothing.

This is also why the receiver choice matters more than it looks: a discrete
four-resistor differential amplifier's CMRR is set by resistor matching, and
ordinary tolerances give figures that collapse in exactly the noisy
environment the balancing exists for.

---

## Component shortlist

Every row is a candidate to verify, not a decision. See the warning at the top
of this document.

| Function | Candidate | What to confirm |
|---|---|---|
| USB controller | **XMOS XU316-1024-QF60B-C24** | ~$9.75, Digi-Key stocked. xcore.ai, 16 logical cores. UAC2 via `lib_xua`. **Not XU208** — see below. Confirm the QF60A/QF60B pinout difference before layout. |
| Converters | **Cirrus Logic CS42448** | 6-in / 8-out in one package — exactly the channel count, one clock domain by construction. Verified from datasheet DS648F5: ADC dynamic range 105 dB differential, THD+N −98 dB, **full-scale input 1.12 × VA Vpp differential**, differential input impedance 29 kΩ, **interchannel gain mismatch 0.1 dB**, no input PGA. `-DQZ` is the automotive grade (−40 to +105 °C) against `-CQZ` commercial (−10 to +70 °C). The Audio Injector Octo is an open-hardware CS42448 layout reference. |
| Mic capsule | **PUI AOM-5024L-HD-R** | ~$4. 14 dBA self-noise (80 dB SNR), −24 ±3 dBV/Pa, **110 dB max SPL** (THD<3%), 20 Hz–20 kHz, 9.7 × 5 mm. Chosen with a headroom trip-wire — see below. Fallback is Primo EM272Z1 (122 dB max SPL, ~$25). |
| In-capsule driver | THAT 1606, or a dual op-amp (OPA1662 class) | Must fit inside the mic body. |
| Line receiver | THAT 1206, or TI INA1650 | CMRR under real-world resistor tolerance. |
| Clock | Single 24.576 MHz oscillator, ~1 ps RMS jitter | Do not buy an exotic clock. What matters here is that all channels share one, which is structural. |
| Mic bias | Dedicated LDO + per-channel RC filter | Bias noise couples directly into signal. |
| Rails | **3.3 V analog and digital, 1.0 V core** | See "Everything runs at 3.3 V" below. No negative rail, no boost converter. |
| Mic connector | Switchcraft TA4F / TA4M mini-XLR | 4-pin: +5 V, GND, HOT, COLD. Locking and small. |
| Mic cable | 4-core shielded instrumentation, ~5 m | Reach every seat from the footwell. |
| Enclosure | Hammond 1455J1201 extruded aluminium | ~120 × 78 × 27 mm. |

### XU316, not XU208

Checked 2026-08-07. XMOS has not published an NRND notice for the XU208, and
it remains distributor-stocked, but three things point the same way:

- **XK-AUDIO-216-MC-AB-L — the XU208-era multichannel dev board — is EOL.**
  That was the Stage-2 de-risking platform in the original plan.
- The current reference platform is **XK-AUDIO-316-MC-AB**, built on
  XU316-1024-TQ128.
- XMOS staff recommend XU316 for new designs in their own forums.

Targeting XU208 would mean porting *away* from the live reference design to
reach a part with no current dev board. XU316-1024-QF60B-C24 is ~$9.75 and
stocked, so there is no cost argument either. The dev board uses the TQ128
package and the custom board would use QF60; that difference is a pin remap,
not a firmware rewrite.

### Everything runs at 3.3 V

USB supplies 5 V, and the CS42448's analog rail wants 5 V for best
performance — but you cannot cleanly regulate 5 V from 5 V. The alternatives
were a boost-then-LDO stage (a switcher next to the analog front end, in a
measurement instrument) or running the analog section at 3.3 V, which the
datasheet permits across a 3.14–5.25 V range with "slightly degraded" analog
performance.

**Run it at 3.3 V.** The degradation is against a 105 dB dynamic-range spec,
and the capsule's own 14 dBA self-noise sits roughly 90 dB above the
converter's floor either way — the ADC is nowhere near being the limiting
element. In exchange: one LDO from USB 5 V, no boost converter, no switcher
near the analog section, and lower power.

This also fixes the required front-end gain at something trivial:

```
capsule       -24 dBV/Pa           = 63.1 mV/Pa
at 110 dB SPL  6.32 Pa             -> 399 mV rms
ADC full scale 1.12 x 3.3 V pp     -> 1.31 V rms  (differential)
                                      ---------
gain needed                           3.3x = 10.3 dB
less 6 dB free from differential drive
                                   -> ~4 dB of actual amplification
```

Four decibels. The front end is a buffer with a trim, which is the strongest
possible confirmation that no PGA belongs in this design.

### Bus-power budget

The open question from the first draft, now closed with datasheet figures.

| Item | Power | Source |
|---|---|---|
| CS42448, all converters active, all rails 5 V | 600 mW typ / 850 mW max | DS648F5 |
| CS42448 estimated at 3.3 V analog | ~400 mW | Extrapolated — **verify** |
| XU316-1024 | ~600 mW | Estimated; see XMOS AN02023 — **verify** |
| Analog front end (4 capsule drivers, 6 receivers) | ~200 mW | Estimated |
| Regulator losses, flash, misc | ~150 mW | Estimated |
| **Total** | **~1.35 W ≈ 270 mA at 5 V** | |

That fits inside USB 2.0's 500 mA with roughly 45% headroom. Two further
levers if the estimates prove optimistic:

1. **Power down the two unused DAC pairs.** We use 4 of 8 DAC channels; the
   Power Control register (02h) has per-pair power-down bits. This is a
   register write, costs nothing, and should have been in the design anyway.
2. **USB-C can advertise more than 500 mA.** The connector choice was made on
   other grounds, but it means the 500 mA figure is a floor rather than a
   ceiling if the host supports it.

### Capsule choice: headroom, not noise

Checked 2026-08-06. The two candidates are **identical where it was assumed
they would differ, and differ where it matters**.

| | PUI AOM-5024L-HD-R | Primo EM272Z1 |
|---|---|---|
| Self-noise | 14 dBA (80 dB SNR) | 14 dBA (80 dB SNR) |
| Sensitivity | −24 ±3 dBV/Pa | −28 ±3 dBV/Pa |
| **Max SPL** | **110 dB** (THD<3%) | **122 dB** |
| Response | 20 Hz – 20 kHz | 20 Hz – 20 kHz ±1.5 dB |
| Body | 9.7 × 5 mm | 10 mm |
| Price / channel | ~$4 | ~$25 |
| Datasheet | Manufacturer PDF, distributor-stocked | Vendor pages only; sources disagree (122 vs 130 dB) |

Self-noise is a non-issue either way: both sit at 14 dBA, a stationary car
interior is 30–40 dBA, and swept-sine deconvolution adds substantial
processing gain on top. **The low end is not the binding constraint.**

The 12 dB of headroom is. Cabin gain gives roughly 12 dB/octave of pressure
rise below the vehicle's cutoff, so a sweep set to 85 dB SPL broadband can
reach 100–105 dB SPL at 30 Hz at the same microphone. 110 dB is a THD<3%
figure, so compression begins well below it — putting the PUI within a few dB
of its knee on subwoofer integration, which is the measurement this tuner
exists to get right.

**This failure mode is invisible to the clip detector.** Safety rule 3 aborts
on clipping, but it watches digital level. A capsule compressing at 105 dB SPL
while the converter reads −25 dBFS reports a clean signal and yields a smooth
low-frequency rolloff that is not real — the same shape, and the same class of
error, as the Windows noise-suppression incident.

Note that the log-sweep method does *not* protect against this, though it
looks as though it should. Farina deconvolution separates harmonic distortion
into impulses arriving ahead of the linear response, which is why swept sine
tolerates a distorting loudspeaker. Compression is not harmonic distortion; it
is level-dependent gain, and the sweep's LF content is both loudest and
slowest, so it compresses differently from the top octave. The method's
distortion immunity buys nothing here.

**Decision: PUI AOM-5024L-HD-R, with an explicit trip-wire.**

Chosen on cost and supply chain. The PUI is a distributor-stocked part with a
revision-controlled manufacturer datasheet; the Primo's specs come from
resellers and *disagree with each other on max SPL* (122 vs 130 dB) — exactly
the situation CLAUDE.md's cite-the-datasheet rule exists to prevent.

**The trip-wire, which must be checked before the capsules are trusted:** run
`measure_level_linearity` at real in-vehicle SPL, at the mic positions that
will actually be used, with the subwoofer active. If any capsule departs from
linear within 10 dB of the intended sweep level, switch the array to EM272Z1
and re-test. Do not treat a bench linearity check as sufficient — the failure
mode is LF-specific and cabin gain is what provokes it.

Buy 6–8 rather than 4. At ~$4 each the spares cost nothing and allow the four
best-matched units to be selected after calibration.

**Do not mix capsule types within the array.** The argument that makes DIY
substitution calibration adequate is that elements sharing a reference and a
procedure share their common-mode error, so relative match stays tight even
though absolute accuracy is only ±1.5–2 dB. Mixing part numbers breaks that,
and relative match is what spatial averaging depends on.

### Assembly: self-reflow, with the risk handled by design

**Decision: assembled in-house with a reflow station and a stencil**, not by a
PCBA service.

The original objection stands on its own terms — a hand-reflowed 0.5 mm QFN
can fail in a way that is indistinguishable from a design error, and debugging
a board when you cannot separate those two is genuinely the worst place to be.
Self-assembly does not remove that risk, so the design has to absorb it:

- **Order five boards and a stencil, and build two.** A second board built
  identically is the control. If both fail the same way it is the design; if
  only one fails it is the assembly. This is the single highest-value
  mitigation and it costs one extra board's worth of parts.
- **Stage the bring-up.** Populate and verify power rails first, with nothing
  else fitted. Then the XMOS and its boot flash — confirm USB enumeration
  before any analog part is on the board. Then the codec. Then the front end.
  A board that enumerates before the analog section exists has already
  eliminated most of the schematic from suspicion.
- **Test points on every rail and every clock**, brought to pads a probe can
  actually reach. Cheap at layout time, impossible to add later.
- **No 0402 passives where 0603 fits.** Board area is not scarce here.

### Package choice under self-assembly

The XU316 is available as **QF60** (QFN-60, 0.5 mm pitch) and **TQ128**
(TQFP-128, 0.4 mm pitch — the package the dev board uses).

Recommendation is **QF60**. Its coarser 0.5 mm pitch is much more forgiving of
paste volume and placement than 0.4 mm, and bridging risk dominates at these
sizes. The cost is that QFN pads are hidden: they cannot be visually inspected
or touched up with an iron, only reworked with hot air.

TQ128 is worth reconsidering if you would rather have inspectable leads, and
it carries a second benefit — it matches the dev board exactly, which removes
the pin-remap step when porting the firmware. It is a legitimate choice; the
deciding factor is which failure you would rather debug.

### Layout notes

Four-layer mixed signal. Star ground with a single-point AGND/DGND join under
the codec. Ferrite isolation between USB 5 V and the analog rail. No charge
pump or boost converter is fitted — see "Everything runs at 3.3 V" — so the
board has no switching node near the analog section other than the 1.0 V core
buck, which should be placed hard against the XMOS and away from the front
end.

---

## Build stages

Ordered so that value lands early and the expensive, irreversible commitment
comes last.

### Stage 1 — Microphones · *startable now*

Build four capsules with in-body balanced drivers, bodies, and cables.

> **The capsule PCB is designed.** Rev A schematic and 4-layer layout, ERC- and
> DRC-clean, with fabrication outputs: **[capsule-board.md](capsule-board.md)**.
> Two findings there change rows in this document:
>
> - **THAT 1606 is eliminated** as the in-capsule driver. Its supply range is
>   ±18 V to ±4 V — a split-supply part needing 8 V minimum, which the single
>   5 V rail down the mic cable cannot provide. The driver is **OPA1662**,
>   whose 3–36 V single-supply range is characterised at 5 V.
> - **The tube body size below is wrong.** The AOM-5024L-HD-R is Ø9.7 ±0.1 mm
>   and will not fit an 8–10 mm body; it needs ~10 mm ID / 12 mm OD.

**These work with any interface, including the Scarlett.** This stage has
immediate value and no dependency on the rest of the design, which is why it
comes first.

It does have software prerequisites, both of which are needed for *any*
multichannel interface, bought or built:

- `tuner.cal` (Milestone 2) — `derive_calibration` and `splice` in
  [substitution.py](../src/tuner/cal/substitution.py) are stubs. Output is one
  REW-format `.cal` file per mic, hashed into provenance via
  `tuner.cal.calfile.file_sha256`.
- `verify_simultaneous_capture` in
  [devices.py](../src/tuner/audio/devices.py) — the Milestone 1 acceptance
  gate, also a stub.

### Stage 2 — XMOS dev board · **decision point**

Buy the **XK-AUDIO-316-MC-AB** (~$245) and bring up the firmware and the
six-channel software path on hardware that is already known to work. This
separates firmware risk from PCB risk completely. An XTAG4 debug adapter is
integrated, so no separate programmer is needed.

The board is 8-in / 8-out line level over TRS, up to 192 kHz — a superset of
what the final device needs, so the entire host-side software path
(`verify_simultaneous_capture`, six-channel capture, inter-channel alignment)
can be written and validated against it before any board is laid out.

**This is where full-custom versus module-integration is decided.** If
firmware bring-up is painful or the toolchain fights back, the fallback is a
commercial class-compliant USB↔I2S bridge plus a converter board, designing
only the analog front end. Retreating at this point costs one dev board.
Retreating after a PCB spin costs months.

### Stage 3 — Custom PCB, rev A

### Stage 4 — Rev B, enclosure, in-vehicle validation

---

## Validation

The board is not working because it enumerates. A device that enumerates and
streams silently-wrong data is the failure mode this whole project is built to
catch, and it has already happened once here — Windows' noise suppression
produced four smooth, plausible, entirely wrong measurements before a level
sweep exposed it.

Acceptance is by measurement, with numeric thresholds fixed in advance.

1. **Electrical loopback flatness and level linearity.** Same procedure that
   characterized the Scarlett (hardware.md, "Measured: Scarlett Solo interim
   rig"), using this project's own engine. Must meet or beat **±0.35 dB** over
   100 Hz – 15 kHz, and pass `require_linear_path` in
   [qa.py](../src/tuner/measure/qa.py).
2. **`verify_simultaneous_capture` on all six inputs.** A partially-working
   interface is worse than a broken one, because it produces data that looks
   fine.
3. **Inter-channel alignment.** Split one signal into all six inputs; measured
   inter-channel delay must be **0 samples to sub-sample precision**. This is
   the test a four-UMIK rig fails catastrophically, and it is the entire
   justification for the build — so it is not optional and not a formality.
4. **Inter-channel drift.** Run ten minutes; the cross-correlation lag between
   channels must not move. One clock means it cannot. Measure it anyway.
5. **Fixed-gain constant**, measured per channel, recorded, and written into
   `Provenance.gains_db` as a board constant.
6. **Self-noise and acoustic overload point**, measured — not taken from the
   capsule datasheet.
7. **Cross-rig agreement.** Measure the same device under test with the
   Scarlett rig and with this one; they must agree within a stated tolerance.
   This is the same argument as the REW golden tests: a new measurement path
   inherits no credibility from the engine it runs, and is trusted only once
   it reproduces a known-good one.

Until item 7 passes, the Scarlett rig remains authoritative.

---

## Bill of materials

Prices are indicative and unverified; check at order time.

### Microphone assemblies — 4 off · **orderable now**

| Item | Qty | Unit | Ext |
|---|---|---|---|
| PUI AOM-5024L-HD-R capsule | 8 | $4 | $32 |
| In-capsule differential driver — **OPA1662DGK** (THAT 1606 ruled out, see above) | 4 | $3 | $12 |
| Capsule PCB, 4-layer, panelized — 8.0 × 48 mm, [designed](capsule-board.md) | 1 | $15 | $15 |
| Tube body + end caps, **~10 mm ID / 12 mm OD** (capsule is Ø9.7 mm) | 4 | $5 | $20 |
| Switchcraft TA4M cable connector | 4 | $5 | $20 |
| 4-core shielded cable, 5 m runs | 20 m | $2/m | $40 |
| Passives, heatshrink, strain relief | — | — | $12 |
| | | | **$151** |

Eight capsules rather than four: spares plus best-four selection after
calibration.

### One-off tooling

| Item | Qty | Unit | Ext |
|---|---|---|---|
| XK-AUDIO-316-MC-AB dev board (XTAG4 integrated) | 1 | $245 | $245 |

Not part of the final rig. Stage-2 only, and the point at which the
full-custom decision can still be reversed cheaply.

### Interface board — 1 off

| Item | Qty | Unit | Ext |
|---|---|---|---|
| XU316-1024-QF60B-C24 | 1 | $9.75 | $10 |
| CS42448-CQZ (or -DQZ automotive) | 1 | ~$15 | $15 |
| QSPI boot flash, 16 Mbit | 1 | $0.60 | $1 |
| 24.576 MHz oscillator, low jitter | 1 | $2.50 | $3 |
| Low-noise 3.3 V LDO, analog | 1 | $3.50 | $4 |
| 3.3 V LDO, digital | 1 | $0.60 | $1 |
| 1.0 V core buck | 1 | $1.20 | $1 |
| INA1650 dual line receiver (4 mic ch) | 2 | $6 | $12 |
| OPA1662 dual op-amp (loopback + spare + output buffers) | 3 | $3 | $9 |
| USB-C receptacle | 1 | $1 | $1 |
| Switchcraft TA4F panel connector | 4 | $6 | $24 |
| RCA output jacks | 4 | $1 | $4 |
| Passives, ferrites, ESD protection | — | — | $15 |
| Passives for a second board (control build) | — | — | $15 |
| PCB, 4-layer, 5 off | 1 | $30 | $30 |
| Framed stencil | 1 | $25 | $25 |
| Second set of ICs for the control build | 1 | ~$45 | $45 |
| Hammond 1455J1201 enclosure | 1 | $25 | $25 |
| Panel machining | 1 | $30 | $30 |
| | | | **~$255** |

Self-assembly removes the ~$100 PCBA line and adds a $25 stencil. The saving
is deliberately spent on a **second complete set of parts**, so two identical
boards can be built — the control that separates an assembly fault from a
design error. That is worth more than the $100 it costs.

Assumes solder paste, reflow station and hot-air rework are already on hand.

### Totals

| | |
|---|---|
| Microphones | $151 |
| Interface board (two builds' worth of parts) | $255 |
| **Rig subtotal** | **$406** |
| Dev board (one-off) | $245 |
| **First-unit total** | **$651** |

Expect a rev B. Budget a second PCB and stencil order (~$55, parts already on
hand from the five-board run) as likely rather than possible.

---

## Open questions

- **QF60A versus QF60B** pinout and peripheral differences — resolve before
  layout.
- **Power estimates for the XU316 and for the CS42448 at 3.3 V analog** are
  extrapolations. Confirm against XMOS AN02023 and by measurement on the dev
  board, which is the cheapest place to find out.
- **Commercial versus automotive CS42448 grade.** `-DQZ` covers −40 to
  +105 °C against `-CQZ`'s −10 to +70 °C. A car interior in summer exceeds
  70 °C, but the interface is a portable instrument, not permanently
  installed. Decide deliberately rather than by default.
- **Capsule headroom.** The PUI trip-wire above is unresolved until measured
  in-vehicle.
- **Capsule long-term stability.** Electrets drift with age and humidity, and
  a car interior swings across a wide temperature range. Since
  `Provenance.temperature_c` already exists to track the *vehicle's* thermal
  response, a microphone that also drifts with temperature is a confound in
  the same term — worth checking each candidate's temperature coefficient.
  Recalibration against the UMIK-1 is cheap once the rig exists, but the
  interval is unknown.
- **Mic body diameter and free-field response.** Body diameter affects
  high-frequency directivity, and `substitution.py` already notes that capsule
  position rather than body position must match during calibration. A swap
  fixture is needed.
