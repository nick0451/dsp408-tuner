# DSP-408 Automated Tuner

Automated acoustic tuning for the **Dayton Audio DSP-408** (4-in / 8-out car audio DSP).

Plays measurement stimuli through the system, captures them with calibrated microphones, derives impulse and frequency responses, fits corrections (gain, delay, crossover, parametric EQ) against a target curve and the chip's resource budget, writes them to the DSP, and re-measures to verify convergence.

This is a measurement and constrained-optimization system. There are no learned components in the tuning path.

## Status

**See [docs/STATE.md](docs/STATE.md) for current state and next steps.**

| Milestone | Scope | State |
|---|---|---|
| M0 | DSP-408 control protocol | **Done.** Decoded; all parameter scalings measured; codec *and session layer* validated against real device traffic |
| M1 | Measurement engine | **Done**, and validated against REW — 0.35 dB max / 0.09 rms over 30 Hz—3.5 kHz, a second implementation sharing no code |
| M2 | Microphone calibration rig | Not started |
| M3 | DSP control backend | **Done, and proven through Stage 6 on hardware.** 2026-08-11: reads the real device clean — 31/31 transactions, firmware `MYDW-AV1.06`, all eight channel records — survives 120 s of link silence, and writes: a fragmented no-op, a real change with verified rollback, then **46 writes across 4 channels** including the backend's own multi-block `write_channel` and a gang write read back holding one tune. Still unproven: preset-recall restore, all eight channels in one run, and writing while audio plays |
| M4 | Closed tuning loop | **Ran end to end on hardware 2026-08-12 and accepted.** `tuner.orchestrate` joins the parts — arm, floor, baseline, fit, write once, verify, settle — with a fingerprinted objective, per-output budgets, driver gangs and acoustically-verified rollback, on an electrical bench rig with the device restored and verified byte-identical afterwards. Rehearsed against the fake through every outcome. Remaining: no microphone has been in the loop, all eight channels in one run, and writing while audio plays |

In place: the measurement signal chain (sweep, deconvolution, gating,
frequency/phase/RT60, spatial averaging, REW cal files), an end-to-end capture
path with safety ramping and provenance, rig verification, the safety limiter,
the improvement invariant, the DSP backend interface with a resource-budget
simulator, a wire-protocol codec **validated byte-for-byte against captured
device traffic**, readers for the vendor backup format and for Android HCI
snoop logs, and a PEQ fitter that solves on the device's real parameter grids.

**This code reads, writes, tunes and rolls back a real DSP-408.** Proven on
hardware through every bring-up stage and a full closed-loop run on
2026-08-12, with the device verified byte-identical afterwards each time.
No microphone has been in the loop yet -- the closed loop ran electrically,
DSP output into a line input.

Also in place as of 2026-08-09: the full DSP control stack — framing, a
transmit allow-list, transports, an in-process fake device, lock-step sessions,
read-modify-write with a journal, and **snapshot/restore, so rollback is now a
mechanism rather than a policy**. Plus target curves, time alignment, and a
bring-up rehearsal that runs the entire first-contact procedure against the fake
including every abort path.

Added by the 2026-08-09 bench session: **preset store and recall**, which is
the device-side rollback the improvement invariant has always described. A
recall turns out to be eight *reads* with the slot in a header field — no
select opcode — which also means a frame that looks like an ordinary read
replaces the entire working tune. The transmit policy had been permitting it.

That session also mapped the crossover slope and alignment bytes and the EQ
band-type byte, all three of which had read as constants on all 112 stored
channel-records. They were not opaque: **the corpus simply had no variation in
them**, because every tune ever saved was Linkwitz-Riley, at 12 or 24 dB per
octave, with no shelves. Sixteen deliberate A/Bs settled what no further
analysis of the backups could have.

Still stubbed: `tuner.audio.devices` and everything in M2.

**Every validation claim here names its reference and its independence.** The
wire protocol and the 296-byte channel readback are checked against evidence
from outside this project — 61 742 frames of captured vendor traffic, and
the vendor app's own backup files. The measurement engine is checked against
REW, an implementation sharing no code with it, over the band where the
*reference* is repeatable enough to serve as one; above 3.5 kHz REW's own
run-to-run scatter (0.370 dB rms) exceeds our disagreement with it, and our
engine repeats to 0.080. See the validation policy in
[CLAUDE.md](CLAUDE.md).

## Install

```bash
pip install -e ".[dev]"
pytest
```

The test suite passes with no audio hardware attached and no DSP connected.

## Hardware

The DSP-408 contains **two** Analog Devices ADAU1701 SigmaDSPs — 48 kHz fixed, 1024 program instructions each, and delay RAM pooled per chip rather than globally. The optimizer treats those limits as hard constraints. Control is over **classic Bluetooth RFCOMM (SPP)** — measured from a capture of the vendor app, correcting an earlier and wrong conclusion that it was BLE. The wire protocol is decoded, implemented in `tuner.dsp.protocol`, and validated against that capture.

See [docs/hardware.md](docs/hardware.md) for the host platform decision, bill of materials, and measured rig characterization. Short version: an N100-class x86 mini-PC with an external class-compliant USB interface. Compute is not the constraint in this project; audio I/O reliability and microphone calibration are.

The four-microphone interface is being **built rather than bought** — see [docs/measurement-interface.md](docs/measurement-interface.md). Commercial multichannel interfaces all carry gain knobs, which turn a provenance field into a remembered number, and none can offer an internal loopback. Measurements today run on a Focusrite Scarlett Solo, and will continue to until the new rig reproduces its results.

## Documentation

- [docs/STATE.md](docs/STATE.md) — **current state, open questions, next steps**
- [CLAUDE.md](CLAUDE.md) — safety rules, unit conventions, hardware constraints
- [docs/hardware.md](docs/hardware.md) — platform, BOM, wiring
- [docs/measurement-interface.md](docs/measurement-interface.md) — the 4-mic interface design and why it is built, not bought
- [docs/measurement-theory.md](docs/measurement-theory.md) — swept-sine method and its limits
- [docs/dsp408-protocol.md](docs/dsp408-protocol.md) — decoded control protocol, its derivation, and the transport/session/scaling measurements
- [docs/board-probing.md](docs/board-probing.md) — what is on the boards, and how to probe them
- [docs/next-bench-session.md](docs/next-bench-session.md) — run sheet for the next hardware session
- [docs/review-2026-08-09.md](docs/review-2026-08-09.md) — adversarial review scorecard, including the claims that were wrong
- [docs/safety.md](docs/safety.md) — why each safety rule exists

## Safety

Measurement sweeps can destroy drivers. Every stimulus passes through `tuner.safety`, starts at −30 dBFS and ramps, and aborts on clipping or DC offset. Channels whose crossover is unknown are treated as tweeters. These rules have no exceptions — see [docs/safety.md](docs/safety.md).

One limit of that limiter is worth stating on the front page: **it caps what we transmit, not what the driver receives.** The DSP's own channel gain and EQ boost are applied downstream of everything this code controls, so a +12 dB band turns a −18 dBFS stimulus into 0 dBFS at the speaker. Subtract the device's gain from the stimulus deliberately.

Writing to the DSP carries its own hazards, because **every write is immediately non-volatile and the device has no undo**. The backend refuses anything the vendor app was never observed doing, caps how many channels one session can touch, requires a verified snapshot on disk before it will write at all, and verifies every write by reading the whole 296-byte channel back. `python tools/dsp408_probe.py rehearse` runs that entire procedure against an in-process fake, including every abort path, before it meets hardware.

## License

MIT — see [LICENSE](LICENSE).

The protocol findings in `docs/` are the result of black-box measurement and
of decoding this project's own captures of the device's traffic. No vendor
source or firmware is redistributed here.
