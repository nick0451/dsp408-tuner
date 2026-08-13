# Probing the DSP-408 boards

Written 2026-08-08 from the operator's own photographs in `Board Images/`,
which supersede the third-party teardown for anything they disagree on.

**Goal, in priority order:**

1. **Channel-to-chip mapping** — which four outputs belong to which ADAU1701.
   This is the last hard blocker on the resource-budget model and the one thing
   `CLAUDE.md` forbids guessing.
2. **The I²C bus** — is it common to both ADAU1701s and the EEPROM? If so, one
   logic-analyzer clip point captures the whole parameter map at boot.
3. **The 2×5 header** — SWD, or an ADI USBi (SigmaStudio) header? This decides
   whether the ST-Link purchase is the right one.
4. **The EEPROM's I²C address** — free, from three strap pins.

---

## What is actually on the DSP daughtercard

Read directly off `2026-08-08_mainboard_top_01.jpg` and
`2026-08-08_mainboard_top_03.jpg` (renamed 2026-08-10; see the naming
convention at the top of `Board Images/`).
Silkscreen: `YDW-DDS480-DSP-CT2 1-XLB-089 180301`.

| Part | Marking | Location in the photos |
|---|---|---|
| **2× ADAU1701JSTZ** | `ADAU1701 JSTZ / 2143 F / 5553414.1` | centre, one upper one lower |
| **Geehy APM32F103** | `Geehy APM32 F103C… / NSP73…D1 / 3141 x227 arm` | lower right, LQFP-48 |
| **4× NE5532** | `NE5532 / 22M A7KR` | left edge, two upper two lower |
| **Atmel serial EEPROM**, SOIC-8 | `ATMLH146 / 2ECL CN / 2146DUG` | bottom centre, beside the MCU |
| 12.288 MHz oscillator | `12.288 MHz` | top centre (256 × 48 kHz) |
| 8.000 MHz crystal | `HQ 8.000M` | bottom right, beside the MCU |
| 1.8 V LDO | `CJT1117B 1.8` | top right |
| 16-pin SOIC, unidentified | — | centre right |
| **Unpopulated 2×5 header** | — | centre, between upper ADAU1701 and MCU |
| Two board-to-board connectors | — | left and right edges, underside |

### Two corrections to the record

**The MCU is a Geehy APM32F103, not an ST STM32F103.** The APM32F103 is a
Chinese pin- and largely register-compatible clone. Consequences: SWD should
still work with an ST-Link, but **published STM32F103 readout-protection
bypasses are not transferable** — different die, different flash controller.
Treat RDP behaviour as unknown rather than as documented.

**There *is* an 8-pin Atmel EEPROM on the DSP card.** The third-party teardown
said there was none, and the inference that the MCU's internal flash must hold
the tune rested on that. It sits beside the MCU rather than beside either
ADAU1701, which suggests MCU config storage rather than an ADAU1701 self-boot
EEPROM — but that is a hypothesis, and step 3 below tests it.

**If the tune lives in that EEPROM it can be read out non-destructively**, which
would hand us the entire preset structure and working-area layout in one go.
That is a far bigger prize than the channel mapping.

---

## Before touching anything

1. **Unplug the wall wart and every cable.** Wait a minute.
2. **Verify the rails are dead** with the meter on DC volts: probe across a bulk
   electrolytic, and between the 1.8 V LDO's output and ground. Expect ~0 V.
   Do not skip this because the LED went out.
3. **Ground yourself.** Both the ADAU1701s and the MCU are CMOS.
4. **Meter in resistance mode, not continuity, for anything through a chip.**
   Continuity mode injects enough current to forward-bias a protection diode on
   an unpowered board, so a beep is *not* proof of a copper connection. Trust
   **< 5 Ω** as a trace; anything higher is going through a component.

### Probe the passive pads, not the QFP pins

The ADAU1701 and the MCU are LQFP-48 on 0.5 mm pitch. A slipped probe bridges
two pins, and on a board you cannot buy a spare for. **Every pin that matters
here runs to a resistor or capacitor pad within a few millimetres**, and those
pads are ten times the size. Probe those. Use the QFP pin itself only to confirm
a pad you have already identified, and only with a fine tip.

---

## How to trace, if you have not done it before

Tracing is not probing random pairs of points. It is: **find a net, walk to the
end of it, cross one component, repeat.** Four things make it work.

### Null your leads first

Touch the two probes together and read. Test leads are typically 0.1–0.5 Ω, and
**that reading is present in every measurement you take.** Subtract it. If
shorted probes read 0.1 Ω, then a 0.1 Ω measurement means **zero** ohms of
actual connection — direct copper, same net.

Some meters have a REL / ZERO button that subtracts it automatically. Use it.

### Find ground before anything else

**This is the trap that wastes an afternoon.** Perhaps half the pads on this
board are ground. Two ground pads read 0 Ω to each other, so if you do not know
which pads are ground, you will "discover" connections everywhere and none of
them mean anything.

Find a known ground — a mounting hole, the metal can of the 8 MHz crystal, a
large copper pour — and confirm two of them read 0 Ω to each other. Then, **any
time you get 0 Ω between two points, check both against ground before believing
it is a signal net.**

### Read the ranges correctly

| Reading (after nulling) | What it means |
|---|---|
| ~0 Ω | same net, direct copper |
| a stable value: 100 Ω, 1 kΩ, 4.7 kΩ … | a resistor between the two points |
| a value that climbs and settles | you are charging a capacitor — not a DC path |
| OL / open / over-range | not connected **on this range** |

**On a manual 600 Ω range, a 4.7 kΩ resistor reads OL — identical to "not
connected".** Before ever concluding "these are not connected", step up through
the ranges or switch to auto-range. Most of what you are looking for between a
DAC and an op-amp is in the hundreds of ohms to low kΩ.

### Walk the net, then cross one component

1. Put one probe on a start pin and leave it there. A clip lead helps.
2. With the other probe, touch nearby pads. Everything reading ~0 Ω is **the
   same net** — the same piece of copper, however far it wanders.
3. A net ends at component pads. To continue, cross *through* a component: a
   resistor gives you its value, a capacitor gives you an open (dead end for
   this purpose).
4. You are now on the next net. Repeat until you arrive somewhere you recognise.

### A shared vocabulary

So findings can be recorded unambiguously:

- **DSP-A** — the upper ADAU1701 as the photos are oriented; **DSP-B** — lower.
- **OA1–OA4** — the four NE5532s, top to bottom down the left edge.
- Pin 1 on every chip here is marked with a dimple or dot, and numbering runs
  **counter-clockwise from pin 1 viewed from above**.
- The dual op-amp pinout is the industry-standard one — 1 OUT-A, 2 IN−A,
  3 IN+A, 4 V−, 5 IN+B, 6 IN−B, 7 OUT-B, 8 V+ — but **confirm it against the
  NE5532 datasheet** rather than trusting this table.

## Measured: the output stage topology

**Probed 2026-08-08.** On all four NE5532s, **pins 3 and 5 read 0.1–0.2 Ω to
the RCA shell ground and to each other** — eight pins, one ground net.

Pins 3 and 5 are the two non-inverting inputs. Two deductions follow, and both
change how the rest of the probing is done:

1. **The op-amps are inverting stages** — almost certainly multiple-feedback
   low-pass reconstruction filters, which is the textbook thing to hang on a
   SigmaDSP DAC output. So the DAC signal arrives at **IN−, pins 2 and 6**,
   through a series resistor. It is *not* at pins 3/5, which are the grounded
   references.
2. **The analog supply is split (±).** An op-amp cannot swing around a
   hard-grounded non-inverting input on a single rail. Consequently **pin 4 is a
   negative rail, not ground**, and no pin on these chips will read 0 Ω to
   ground except 3 and 5. Absence of a grounded supply pin is the expected
   result here, not a failed measurement.

This was reached by reductio, and it is safe: op-amp outputs cannot be
DC-shorted to ground in a unit that produces audio, and four chips do not share
an identical fault.

### Pin map, once pins 3 and 5 are located

The two grounded pins sit diagonally adjacent near one end of the package.

```
        far end
   1  ┌──────────┐  8        1, 7 -- outputs      -> board-to-board connector
   2  │          │  7        2, 6 -- DAC inputs   -> series R -> ADAU1701
   3  │  NE5532  │  6        3, 5 -- grounded IN+ (measured)
   4  └──────────┘  5        4, 8 -- split supply rails
        near end
```

- **Pin 4** is the corner pin at the near end, in the same column as pin 3.
- **Pin 6** is directly across from pin 3.
- **Pin 2** is directly across from pin 7.
- **Pins 3 and 5 double as a local ground reference** at each chip, which saves
  reaching back to an RCA shell for every orientation check.

## Session result, 2026-08-08

### Established

| Finding | How |
|---|---|
| MCU is a **Geehy APM32F103**, not ST | package marking |
| An **8-pin Atmel EEPROM is present** on the DSP card | package marking |
| Silkscreen `YDW-DDS480-DSP-CT2 1-XLB-089 180301` | photograph |
| All four NE5532s have **pins 3 and 5 hard-grounded** (0.1–0.2 Ω), one ground net across 8 pins | meter |
| Therefore: **inverting MFB reconstruction filters**, DAC arrives at pins 2 and 6 | deduction from the above |
| Therefore: **split (±) analog supply**, pin 4 is a negative rail not ground | deduction |
| **Outputs are AC-coupled** — no DC path from op-amp to RCA centre pin | meter, both modes |
| Daughtercard is **removable**, two board-to-board connectors | photograph |
| Filter network reads 4 kΩ–15 kΩ in-circuit between pin 2 and pads toward DSP-A | meter |

### Not established: the channel-to-chip mapping

**Tracing with a meter did not resolve it, and no guess is recorded.** What went
wrong is worth writing down so the next attempt is not a repeat:

- **In-circuit resistance is a parallel combination**, not a component value.
  With a feedback resistor, an input resistor and the op-amp's own internals all
  bridging the network, a 4 kΩ or 15 kΩ reading is consistent with the
  hypothesis without confirming it. Only a **~0 Ω hop** is unambiguous, because
  only direct copper reads zero.
- The decisive measurement was never reached: walk the pin-2 net to its furthest
  pad, cross the single series resistor, then look for **0 Ω from that far pad
  to a pin on one ADAU1701 and a much higher reading to the other**. It is a
  *comparison* between the two chips, not an absolute reading against one.
- The resistor cluster above the NE5532s is dense and fine-pitched, and a
  600 Ω-floor meter with no REL and no auto-range makes every reading a
  two-step process.

**One suggestive but non-conclusive observation:** OA1's pin-2 net extends
rightward, toward DSP-A. That is a hint, not a result — the layout already
*looked* like upper-pair-to-upper-chip before any probing, and confirming a
prior with an ambiguous measurement is exactly the trap `CLAUDE.md` forbids
here. It is recorded so a future session has somewhere to start, and must not be
promoted to an answer without the 0 Ω hop.

### Do it differently next time

Ranked by likelihood of actually finishing the job:

1. **Capture the I²C bus at boot.** The MCU addresses each ADAU1701 separately
   and downloads each one's program and parameters. That capture *names* the
   mapping, from the device's own behaviour rather than from inferred copper.
   Needs the ~$10 analyzer and the bus check in Step 2 below. This is now the
   recommended route.
2. **Read the ADAU1701 address straps** first — two pins per chip, large-ish
   pads, no ambiguity. Knowing the two addresses makes the capture readable.
3. **Better instruments before more tracing**: auto-ranging meter with a REL
   button, fine-tip probes, and a clip lead so one hand is free. The method was
   sound; the tooling was the bottleneck.

## Step 1 — Channel-to-chip mapping

**The daughtercard unplugs from the mainboard.** That is the whole trick: it
splits an awkward trace through two boards into two easy ones on separate,
unpowered, bare boards.

### 1a. On the daughtercard alone: chip → connector pin

Each NE5532 is a dual op-amp, so four of them buffer eight channels, two
channels each. Two NE5532s per ADAU1701.

For each of the four NE5532s:

1. Find its two **output** pins (SOIC-8 pins 1 and 7 — verify against the
   NE5532 datasheet, do not take it from here).
2. From each output pin, find which **board-to-board connector pin** it reaches.
   Resistance mode; expect a few ohms, possibly through a small series resistor.
   There may be a DC-blocking capacitor in the path — if you get an open, probe
   from the far side of the nearest series capacitor instead.
3. From each NE5532 **input** pin (pins 2/3 and 5/6), trace back toward the
   nearer ADAU1701. This hop runs through the reconstruction filter, so **expect
   a resistor value, not a short** — a reading of a few hundred Ω to a few kΩ is
   the positive result here. An open means you are on the wrong pin.
4. Record which ADAU1701 — upper or lower as the photos are oriented — that
   NE5532 belongs to.

You only need four determinations, not eight: once you know which chip drives
each NE5532, both of its channels follow.

> **The layout makes the answer look obvious** — two NE5532s sit beside the
> upper ADAU1701 and two beside the lower one. Verify all four anyway; a
> plausible-wrong mapping is harder to notice than a visibly wrong one.
>
> **Superseded 2026-08-11.** This continuity route was never needed: the
> mapping was settled from the ADAU control bus instead, by stepping each
> output's gain and recording which chip received the write. `0x37` drives
> outputs 1-4, `0x35` drives 5-8. The procedure below is kept as an
> independent second route should the first ever be doubted.

### 1b. On the mainboard alone: connector pin → RCA jack

For each of the eight output RCA jacks, buzz the centre pin back to a
daughtercard connector pin. Expect a series DC-blocking capacitor, so probe from
the cap's far side if the direct path reads open.

### 1c. Compose

Chip ↔ NE5532 ↔ connector pin ↔ output number. Record all eight, then compare
against the expected 1–4 / 5–8.

---

## Step 2 — The I²C bus

Highest value per minute of any step here, because it decides whether a ~$10
logic analyzer can capture the entire parameter map.

The 24Cxx SOIC-8 pinout is standard — **verify against the datasheet** — but is
conventionally: 1–3 `A0`/`A1`/`A2`, 4 `GND`, 5 `SDA`, 6 `SCL`, 7 `WP`, 8 `VCC`.

1. From EEPROM pin 5 (SDA) and pin 6 (SCL), find which **MCU** pins they reach.
2. From the same two nets, check whether they also reach **both ADAU1701s**.
3. Look for two pull-up resistors from those nets to 3.3 V.

**If SDA and SCL are common to the MCU, both ADAU1701s and the EEPROM**, then a
single clip point sees: the boot-time program download to each DSP, every
parameter write, and every EEPROM access. That is the whole system on two wires.

**If the EEPROM is on a separate bus from the ADAU1701s**, that is informative
too — it would mean the EEPROM is the MCU's private store, strengthening the
case that the tune lives there.

### The EEPROM's address, for free

Probe EEPROM pins 1, 2 and 3 to ground and to VCC. Each will be strapped to one
or the other, and those three bits are the low bits of its I²C address. Write
them down — it costs nothing now and saves guessing later.

### The ADAU1701 addresses, also for free

Each ADAU1701 has address-select strap pins. Look them up in the datasheet, then
read the straps on both chips. **This gives an independent route to the channel
mapping**: once the two DSPs have known, distinct I²C addresses, a logic-analyzer
capture of the boot sequence shows which address receives which channel's
parameters. That is arguably stronger evidence than continuity tracing, because
it observes the device's own behaviour rather than the wiring it implies.

---

## Step 3 — What the 2×5 header is

Ten plated through-holes, centre of the board, visible from both faces. Two
candidates, and they lead to different purchases.

**Test for ARM SWD.** Buzz each of the ten pads against:

- MCU `NRST`
- MCU `PA13` (SWDIO) and `PA14` (SWCLK) — verify pin numbers against the
  APM32F103 datasheet
- GND
- 3.3 V

Finding those five means it is a Cortex debug header and the ST-Link (~$3) is
the right buy.

**Test for an ADI USBi / SigmaStudio header.** Buzz the same ten pads against
the I²C nets found in step 2, and against each ADAU1701's reset pin. A header
carrying I²C plus DSP reset is a SigmaStudio programming header, which is a
different and much more powerful thing — it would allow loading our own DSP
program rather than driving the vendor's.

They are distinguishable by exactly this test, and the answer decides the tool.

---

## Step 4 — Record it

Fill this in as you go and I will turn it into a permanent map:

| Output | RCA → connector pin | Connector pin → NE5532 | NE5532 → ADAU1701 |
|---|---|---|---|
| 1 | | | |
| … | | | |

| Question | Answer |
|---|---|
| SDA reaches | MCU pin ?, ADAU1701 #1 ?, #2 ?, EEPROM ? |
| SCL reaches | |
| EEPROM A0/A1/A2 straps | |
| ADAU1701 #1 / #2 address straps | |
| 2×5 header identity | SWD / USBi / neither |

---

## What not to do

- **No power on while probing.** There is no measurement in this document that
  needs the board live.
- **Do not probe the QFP pins directly** where a passive pad carries the same
  net. See above.
- **Do not solder anything yet.** Every question here is answerable with a meter
  tip. The logic-analyzer tap comes after the bus is understood, and the header
  may make it solder-free.
