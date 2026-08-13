# Target deployment: a Pi, a UMIK, and a browser

Decided 2026-08-13. The tuner moves off a Windows desktop and onto a Raspberry
Pi carried to the car:

```
  phone / laptop  --wifi-->  Pi  --USB--> UMIK-1          (capture)
      (browser)               |  --USB--> audio interface (stimulus) --> DSP RCA in
                              |  --BT RFCOMM-->  DSP-408  (control)
```

The user opens a page served by the Pi, tells it to connect to the DSP, picks
a tune, and starts it. The Pi does the rest.

## Two decisions, and why

### Audio output stays on USB

**Not the Pi's 3.5mm jack.** On a Pi 5 there is no jack — it was removed for
the PCIe slot. On a Pi 4 there is one, and it is not a DAC: a PWM signal from
a CMOS buffer into an RC filter, rolling off around 19 kHz, whose distortion
comes from the driver's output voltage having a **quadratic relationship with
output current** — that is, it depends on the load.

Three reasons that is disqualifying *here*, as opposed to merely mediocre:

- **The stimulus is the measurement's denominator.** Every magnitude we report
  is captured-over-generated. A load-dependent nonlinearity in the output puts
  the host's own distortion inside `require_linear_path`, which is the check
  that exists to catch compressors and AGC — and it cannot tell them apart.
- **It collides with the timing reference.** The acoustic reference chirp is
  5–20 kHz, and a ~19 kHz cutoff clips it. Bandwidth is what sets arrival
  resolution, which is the thing car-audio practice says decides whether an
  acoustic reference survives a cabin at all.
- **`CLAUDE.md` already forbids the shape of it**, in a rule written about
  microphone preamps: audio hardware stays external and class-compliant over
  USB, because sensitive analog sharing rails with a busy SoC in a 12 V
  environment means alternator whine and ground loops. An analog *output* has
  the same exposure.

Keeping USB also preserves something the UMIK can never provide: an
**electrical loopback**, which every bench known-answer test and
`predict-check` depend on. A microphone-only rig cannot validate itself.

### The browser is an observer, never a participant

A run is `TuneRun.execute()` — arm, measure, fit, write, verify, settle, with
rollback in a `finally`. The web layer **starts** one and then watches.

- POST starts a run in a background worker; the page polls or streams
  `StageRecord`s.
- The run owns its own abort and rollback. Closing the tab does nothing.
- No stage waits on the browser to advance it, and no lock is held by a
  socket.

This is not just tidiness. The Pi's Wi-Fi and Bluetooth **share one radio and
one antenna**, so a 2.4 GHz access point and an RFCOMM control link are in
direct coexistence conflict. Dropped links are expected, not exceptional, and
a dropped link must never be able to strand a run between the write and the
verify — on a device with no undo.

> ⚠ **`move_microphone()` currently blocks.** A multi-seat run prompts the
> operator and waits. Under a remote UI that is a run waiting on a browser
> that may never come back. It needs a timeout that aborts and rolls back,
> rather than hanging with the car half-written.

## What the topology introduces

| | |
|---|---|
| **Wi-Fi/BT coexistence** | One combo radio. Prefer a 5 GHz AP, an external BT dongle, or joining an existing network rather than hosting one |
| **The UI phone is a hazard** | Measured: a phone with the vendor app open makes RFCOMM connect **time out**. The device the operator is holding is now the most likely thing to break the control link. The app should detect and say so |
| **Operator declarations become form fields** | `DriverCeiling.basis`, `setup_token`, `NoIsolation.basis`, the gang basis. **Pre-fill nothing that is a claim about the physical world** — a remembered default silently turns a declaration into a constant, which is the exact failure the token was built to prevent |
| **Fit time moves onto ARM** | ~5.4 s per channel on the desktop. Measure it on the Pi rather than guess; it lands inside the run's wall clock, which the floor's `span_s` now reports against |

## 🔬 Open, and settle it by measurement: the DSP is itself a USB audio device

Device enumeration on 2026-08-13 showed **`Speakers (DSP-408)`, Windows
WASAPI, 48 kHz** — the DSP-408 presents a USB audio output endpoint. That is a
potential *digital* stimulus path with no analog stage between host and DSP at
all, which would be strictly better than 3.5mm→RCA and costs nothing.

It is not free of questions, and none should be assumed:

1. **Does it reach the same input section?** Everything measured so far went
   through the RCA analog inputs, and block 33's mixer describes inputs 1–4.
   A USB source may be a fifth input, or may map onto the same four.
2. **Does it collide with the control link?** Measured arbitration signature:
   *the vendor app over USB-B opens the RFCOMM link and then produces total
   silence.* Streaming USB audio into the DSP while holding an RFCOMM control
   link may trip the same arbitration. This is the one that would waste a
   bench session if assumed.
3. **It is an unvalidated signal path** and would need its own known-answer
   check before any tune taken through it is trusted.

Worth an hour on the bench before committing to a cable.

## Porting checklist

Nothing in `orchestrate`, `optimize`, `dsp` (above the transport) or `safety`
is platform-dependent. What is:

- [ ] **`RfcommSocketTransport` on BlueZ.** Same `AF_BLUETOOTH` /
      `BTPROTO_RFCOMM` call that every hardware run used, but a different
      stack. Re-run the bring-up ladder from `dsp408_probe enumerate`.
- [ ] **Device selection under ALSA.** The "never select by index" rule
      stands; the host-API-qualified names do not exist. Needs a Linux
      equivalent and its own ambiguity refusal.
- [ ] **`_extra_settings` is WASAPI-only.** Exclusive mode is a Windows
      concept; on ALSA a `hw:` device is exclusive by nature. `SplitDevices`
      needs a Linux branch.
- [ ] **Re-measure the repeatability floor and level linearity** on the new
      host. Both are per-session quantities and neither survives a change of
      hardware.
- [ ] **Re-run the REW golden comparison**, or state plainly that the
      independent validation belongs to the Windows/Scarlett rig and has not
      been re-established.
- [ ] Time the fit on the target Pi.
