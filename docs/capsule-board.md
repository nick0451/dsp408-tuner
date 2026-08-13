# The microphone capsule board

**Status: rev A designed, not fabricated, not built.** Nothing here has been
measured. Every value below is a design intent to be verified on the bench.

This is Stage 1 of [measurement-interface.md](measurement-interface.md) — the
in-body PCB that carries a PUI AOM-5024L-HD-R electret capsule and drives its
signal down ~5 m of cable as a balanced pair. It is deliberately the first thing
built because **it works with any interface, including the existing Scarlett
rig**, and so has no dependency on the XMOS/CS42448 interface board.

KiCad project: [`hardware/capsule-board/`](../hardware/capsule-board/).

---

## What it does

```
capsule ──> load resistor ──> AC couple ──> unity buffer  ──> 100R ──> HOT
  (MK1)        (R5)             (C5)          (U1A)                    
                                    └──────> unity inverter ──> 100R ──> COLD
                                              (U1B)
```

One capsule, one dual op-amp, a mid-rail reference, and a filtered bias supply.
Differential gain is **×2 (+6 dB)** — each phase is unity, and the balanced pair
gives the 6 dB for free.

### Why unity gain here and not the ~4 dB the interface doc computes

[measurement-interface.md](measurement-interface.md) computes a total ×3.3
(10.3 dB) requirement, of which 6 dB comes free from differential drive, leaving
~4 dB of real amplification. **That 4 dB is deliberately not on this board.** It
belongs at the interface, where per-channel fixed gain is calibrated and written
into `Provenance.gains_db` as a board constant. Putting it here would mean four
separately-toleranced gain stages that all have to match each other.

Keeping this board unity makes it a pure balanced line driver with one job. The
gain-setting resistors (R7/R8) are a matched pair, so if the split ever needs to
change it is a resistor swap, not a redesign.

---

## Verified component facts

Per CLAUDE.md, figures are cited, not remembered.

### PUI AOM-5024L-HD-R — datasheet rev `-`, 6 Jun 2017

| Parameter | Value |
|---|---|
| Sensitivity (1 kHz @ 50 cm, 0 dB = 1 V/Pa) | −24 ±3 dB |
| **Rated voltage** | **3 VDC** |
| Operating voltage range | 1 – 10 VDC |
| Output impedance @ 1 kHz | 2.2 kΩ |
| Current consumption (3 V, 2.2 kΩ R<sub>L</sub>) | 500 µA |
| SNR (1 kHz, 94 dB, A-wtd) | 80 dB |
| Sensitivity change, 3 V<sub>S</sub> → 2 V<sub>S</sub> | −3 dB |
| Max SPL (THD < 3 %) | 110 dB |
| **Operating temperature** | **−30 to +70 °C** |
| Body | Ø9.7 ±0.1 mm × 5 ±0.2 mm |

The datasheet's **Recommended Drive Circuit** is R<sub>L</sub> = 2.2 kΩ from
+V<sub>S</sub> = 3.0 V to the (+) terminal, output tapped at (+) through a
series capacitor, (−) to ground. The capsule has two rear terminals: **(+) is
the FET drain/output, (−) is the case/ground.**

### TI OPA1662 — SBOS489A, Dec 2011, revised Dec 2024

| Parameter | Value |
|---|---|
| **Supply range** | **±1.5 to ±18 V, or 3 to 36 V single** |
| V<sub>CM</sub> range at V<sub>S</sub> = 5 V | (V−) + 0.5 to (V+) − 1 → **0.5 – 4.0 V** |
| Output swing at V<sub>S</sub> = 5 V, R<sub>L</sub> = 2 kΩ | (V−) + 0.6 to (V+) − 0.6 → **0.6 – 4.4 V** |
| Input voltage noise density | 3.3 nV/√Hz @ 1 kHz, 5 nV/√Hz @ 100 Hz |
| Quiescent current | 1.4 mA typ / 1.7 mA max per channel |
| **Capacitive load drive** | **200 pF** |
| Packages | SOIC-8 (D), VSSOP-8 (DGK) |

The datasheet carries a dedicated "Electrical Characteristics: V<sub>S</sub> = 5 V"
table, so single-5 V operation is characterised, not merely permitted.

### THAT 1606 is eliminated

Its allowable supply range is **±18 V to ±4 V** — a split-supply part needing
8 V total minimum. It cannot run from the single 5 V rail this design delivers
down the mic cable. The interface doc lists it as a candidate; this closes that
question.

---

## Why the output isolation resistors are not optional

OPA1662's specified capacitive load drive is **200 pF**. Five metres of
4-core shielded instrumentation cable will exceed that. R6 and R9 (100 Ω, in
series with each output, outside the feedback loop) decouple the amplifier from
the cable capacitance. Without them the driver can peak or oscillate into its
own cable — a fault that would show up as a high-frequency response anomaly and
be very easy to mistake for an acoustic one.

They are a matched pair for the same reason R7/R8 are: asymmetry between HOT and
COLD source impedance degrades the balance the whole cabling scheme exists for.

---

## Circuit values and rationale

| Ref | Value | Purpose |
|---|---|---|
| R5 | 2k2 | Capsule load. **The datasheet's recommended R<sub>L</sub>.** |
| R1 / C3 / C4 | 1k5 / 10 µF / 100 n | Bias RC filter, f<sub>c</sub> ≈ 10.6 Hz |
| C5 | 1 µF | Signal AC coupling |
| R2 | 47 k | Input bias to V<sub>REF</sub>; with C5 gives f<sub>c</sub> ≈ 3.4 Hz |
| R3 / R4 / C6 / C7 | 47 k / 47 k / 10 µF / 100 n | Mid-rail V<sub>REF</sub> = 2.5 V |
| R7 / R8 | 10 k 0.1 % | Unity inverter for the COLD phase |
| R6 / R9 | 100 R | Output isolation (see above) |
| FB1 / C1 / C2 | ferrite / 10 µF / 100 n | Supply entry filter and U1 decoupling |
| R10 | 0 R **DNP** | Optional shield-to-AGND link, normally lifted |

**Bias RC sizing.** Noise on the bias rail couples straight into the signal:
R5 connects V<sub>BIAS</sub> to the drain directly, and the capsule's own output
impedance is also 2.2 kΩ, so rail noise divides by only about 2. R1/C3 puts the
filter corner an octave below the audio band.

**Operating point (calculated, must be measured).** At the datasheet's 500 µA:
V<sub>BIAS</sub> ≈ 5 − (500 µA × 1k5) ≈ 4.25 V, drain ≈ 4.25 − (500 µA × 2k2)
≈ 3.15 V. At 110 dB SPL the capsule delivers ~399 mV rms = 564 mV peak, against
~1.1 V of headroom to the rail. **TP3 exists to check this** — the FET current
is not a tight spec and R5 is the adjustment if it measures marginal.

**Headroom ordering.** Each phase swings ~399 mV rms at the capsule's 110 dB
overload point, well inside the OPA1662's 0.6–4.4 V window. The capsule
therefore reaches its limit before the driver does, which is the correct
ordering — but note the capsule's compression is the *invisible* failure mode
that measurement-interface.md flags, so this does not remove the trip-wire.

**Outputs are DC-coupled** at a 2.5 V common mode. The receiving differential
line receiver rejects that, and it avoids putting an electrolytic or a
voltage-coefficient-prone ceramic in the signal path.

---

## Mechanical — this contradicts the interface BOM

**The AOM-5024L-HD-R is Ø9.7 ±0.1 mm.** The bill of materials in
[measurement-interface.md](measurement-interface.md) specifies a
"Tube body + end caps, 8–10 mm", which **cannot accept this capsule**. The body
needs roughly **10 mm ID / 12 mm OD**.

The board is designed against a 10 mm ID:

| | |
|---|---|
| Board | 8.0 × 48.0 mm, 1 mm rounded corners |
| Stackup | **4-layer**: F.Cu signal / In1.Cu GND / In2.Cu signal / B.Cu GND |
| Track / clearance | 0.2 mm / 0.2 mm (0.25 mm copper-to-edge) |
| Vias | 0.6 mm pad / 0.3 mm drill, through |
| Assembly | Single-sided, top only |

The capsule connects by two short flying leads to plated holes at the front
(MK1); the cable solders to five pads at the rear (J1: +5 V, AGND, HOT, COLD,
SHIELD).

### Why 4 layers on a board this small

Two layers do not work here. On an 8 mm wide board, F.Cu offers only about three
usable vertical channels once the two component columns are placed, and six nets
(V5, VBIAS, VREF, OUT_A, HOT, COLD) need to run most of the board's length. The
only 2-layer solution cuts the ground plane, and cutting the plane under a
high-impedance electret front end is the wrong trade.

The chosen stack puts the long analog runs on In2.Cu, buried between two ground
planes. In1.Cu is left completely unbroken (287 mm² of solid copper) as the
reference plane directly under the F.Cu signal layer.

The alternative — a single column of parts on a ~80 mm 2-layer board — was
considered and rejected in favour of keeping the mic body short.

---

## Verification status

| Check | Result |
|---|---|
| ERC | **0 violations** |
| DRC (clearance, edge, hole) | **0 violations** |
| Unconnected items | **0** |
| Schematic parity | 29 remaining, all benign metadata: 25 empty `Description` fields, 4 test-point "exclude from BOM" flags |

Routed with Freerouting 2.1.0 at 0.25 mm clearance, then hand-checked. 52 vias
(14 signal, 38 ground stitching). Ground stitching was placed by a
collision-checked script — a through via penetrates the whole stackup, so a
candidate has to clear every non-ground track, via and pad on *every* copper
layer, not just the one you think you are on.

Fabrication outputs (4-layer gerbers, separate PTH/NPTH Excellon drill, board
and schematic PDFs, BOM) are in
[`hardware/capsule-board/fab/`](../hardware/capsule-board/fab/).

---

## Bring-up order

Staged so that a fault is localised before the next thing is fitted, mirroring
the interface board's staged bring-up.

1. **Bare board.** Continuity check +5 V to AGND — must be open.
2. **Power section only** (FB1, C1, C2). Apply 5 V, confirm TP1 ≈ 5 V.
3. **Bias and reference** (R1, C3, C4, R3, R4, C6, C7). TP2 ≈ 5 V unloaded,
   TP4 ≈ 2.5 V.
4. **U1 and gain resistors.** Confirm both outputs sit at ≈ V<sub>REF</sub>.
5. **Capsule.** TP2 should now fall to ≈ 4.25 V and **TP3 should read ≈ 3.15 V**.
   A TP3 reading near the rail or near ground means the capsule is not
   conducting as expected — stop and check orientation before trusting anything.
6. **Acoustic.** HOT and COLD must be equal in amplitude and opposite in phase.

Build at least two boards. Per the interface doc's reasoning, a second board
built identically is the control that separates an assembly fault from a design
error — and that argument applies with more force here, because this board is
the one that gets built four times.

---

## Open questions

- **Operating point is calculated, not measured.** The 500 µA figure is a
  datasheet typical at 3 V; at 5 V through 1k5 + 2k2 it will differ. TP3 settles
  it. R5 is the adjustment.
- **Sensitivity at 5 V bias is unknown.** The −24 dBV/Pa spec is given at 3 V.
  The datasheet only characterises the downward direction (−3 dB from 3 V to
  2 V). This does not affect absolute accuracy — every capsule is calibrated
  against the UMIK-1 anyway — but it does affect the headroom sums above.
- **Capsule operating temperature is −30 to +70 °C.** A car interior in summer
  can exceed +70 °C. The mics are portable instruments rather than permanently
  installed, so this is probably acceptable, but it is the same open question
  the interface doc raises for the CS42448 grade, and it should be decided
  deliberately rather than by default.
- **Shield termination.** R10 is fitted as DNP so the shield lands at the
  interface end only. Whether the metal tube body wants a connection to AGND at
  the capsule end is a decision to make with a real cable and a real vehicle.
- **1 µF X7R for C5.** Sees little AC voltage (1.1 kΩ source into 47 kΩ), so
  voltage-coefficient distortion should be negligible — but a film part is a
  drop-in if a distortion measurement says otherwise.
- **Fab capability.** 0.25 mm copper-to-edge and 0.3 mm drill are assumed.
  Confirm against the chosen fab's DRC before ordering.
