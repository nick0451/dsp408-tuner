# The UI question register

Open assumptions, and whether the operator can settle them from the vendor app
in about a minute. **Work this list at the start of a session, before planning
anything that rests on one of these.** The rule and its rationale are in
`CLAUDE.md` under "Check the vendor UI *first*".

Batch the questions. A question per hour trains the operator to skim, and the
value sits in the ones nobody thought to ask.

**Evidence grades**, recorded with every answer: a saved `.DDP` beats a
screenshot beats a recollection. They are not interchangeable.

**Which app matters.** iOS and Android/Windows render the same parameter
differently and expose different sections. Every answer names the app.

---

## Open — ask these next

| # | Question | What it settles | Blocks |
|---|---|---|---|
| 1 | **Band 1 on Windows/Android — does it offer PEQ / low shelf, the way band 10 offers PEQ / high shelf?** | Three `.DDP` files carry a low shelf on band 1 at 31 Hz, so it almost certainly does. Currently the only *inferred* corner of the shelf picture; everything else is observed | Nothing urgent. Completeness of the shelf model |
| 2 | **Does preset "recall" warn or confirm before overwriting the working area?** | A recall replaces all eight channels with no undo. If the app warns, the operator has a habit we should not silently break | The fast rollback path |
| 3 | **Master volume** — what range does it show, and is it per-preset or global? | Read from `dt9` channel 5. Whether a preset recall moves it decides if it belongs in the snapshot's comparable set | Provenance comparability |

## Not UI-answerable

Listed so they are not repeatedly re-triaged.

| Question | Why the UI cannot settle it | Cheapest real route |
|---|---|---|
| What does the device do with an **out-of-range delay**? | The app clamps; that tells us about the app, not the device | One bounded bench write |
| Does the **`0x52` error reply** ever occur, and what does it mean? | Never observed in 61 742 captured frames | A deliberate read probe of an out-of-range `data_id` |
| **Program space** headroom | Not a resource we allocate; the firmware is fixed | Nothing. Retired |
| Are blocks **34/35** truly inert? | No UI exposes them, so the UI cannot show them changing | Nothing worth doing. They are refused either way |

---

## Closed, with what it cost and what it saved

Kept because the pattern is the argument for the practice.

### Round 1, 2026-08-12 — ten questions, one sitting

| Answer | Effect |
|---|---|
| **EQ gain range is ±12 dB** | Confirms `max_cut_db = 12.0`. The device also permits +12 boost; our +3 cap is a deliberate choice of ours, not a device limit, and should say so |
| **The control is labelled "Q" in both apps** | The vendor thinks in Q and stores bandwidth; our `q_from_bw_raw` / `bw_raw_for_q` sit on the same boundary the app does. Range still unknown — now question 1 |
| **Frequency accepts typed values and rounds to the stored quantisation** | Confirms fitting frequency continuously. Also confirms the checksum-nudge strategy, which needs 1 Hz steps to be real |
| **No shelf control seen; 10 bands with frequency, gain, Q** | *Contradicted by the corpus* — see question 2. The most valuable answer of the round, because it was wrong in a way that pointed somewhere specific |
| **Polarity is exposed per channel as 0 / 180** | Confirms `polar` is a two-state field. Relevant to gangs: a polarity mismatch inside one enclosure is cancellation |
| **Delay is numeric entry only, no slider** | Full resolution is reachable; 384 samples is enterable exactly |
| **No speaker-type control at all — "completely agnostic"** | `spk_type` is not operator-settable. Carried blind through read-modify-write, which is correct and now known to be sufficient |
| **6 preset slots, worded "save" and "recall"** | Confirms slots 1–6 are real and 7–15 are the stale-name artefact. Matches the protocol's `PRESET_SLOT_MAX` |
| **The Windows app cannot store device presets** | The rollback path exists only on Android/iOS. Worth knowing before relying on the operator having it to hand |
| **Screenshots of both mixers** (`DSP Windows Application Screenshots/`) | **Decoded block 33** and **killed the `DataType 3` lead** — see below |

### The 30-band question, answered by measurement 2026-08-12

**Ten bands, not thirty.** Slot 1 realises a written −6 dB band to 0.062 dB
rms; slots 11 and 31 do nothing at all, across four runs, while reading back
byte-exact. Firmware `MYDW-AV1.06`. Full account in `CLAUDE.md`.

The community belief is reasonable — 31 slots are addressable, all accept
writes, all read back faithfully — and the failure is **silent**: nothing at
the protocol layer distinguishes a stored-but-inert band from a working one.
Only a measurement can.

Started life in this file as "no reason to want them", which was wrong twice
over: it is 3× the correction resource, and it turned out to be the headline
finding of the week.

### Round 2, same day — the two follow-ups

| Answer | Effect |
|---|---|
| **Q ranges 0.404 to 28.852** | `bw_raw` is an integer in **[0, 295]** = 0.05 to 3.00 octaves. Both endpoints match `q_from_bw_raw` to three decimals, and the low end is *exactly* 3.000 octaves — a **third independent confirmation of `octaves = (raw + 5)/100`**, and the one that pins the `+5` offset |
| **Band 10 offers PEQ or HS on Windows/Android; iOS offers neither on band 1 or 10** | The shelf refusal in `_plan_peq` is **live, not theoretical** — an operator can make a channel unfittable from the Windows app. And iOS is confirmed as *not* a superset: second divergence after the gain display |

Round 1's "no shelf options" was wrong, and the corpus disagreeing with it is
what located the feature: two bands out of ten, two apps out of three. **A
confirmation would have closed the question; the contradiction found its
shape.**

### What round 1 actually bought

- **Block 33 = MIX, decoded.** Eight bytes, one per input, 0–100. Only four used on a 4-input device. The live routing read out of it matches `docs/hardware.md`'s reachability table, which was derived independently on the bench by sweeping for silence.
- **The `DataType 3` lead is dead.** "iOS shows input values 0–100" was recorded as the first evidence of a vendor path to the unmapped input section. It was the mixer. Both apps have it. One screenshot retired a day-old lead.
- **Blocks 34/35 reframed.** Constant on every channel, no UI anywhere, and block 33 holding eight input slots on a four-input device — all pointing at a shared codebase for a larger sibling product. The refusal stands; the reason is better.
- **One contradiction found**, with a precise place to look. Worth more than the nine confirmations.

### Earlier, incidental

| Answer | Replaced |
|---|---|
| **Delay maxes at 8 ms per channel, settable on all outputs at once** (2026-08-12) | An SWD dump, a datasheet hunt, or a bench binary search — and corrected the shared-pool model to static per-channel |
| **10 PEQ bands per channel, not 31** (2026-08-12) | Confirmed `max_peq_per_channel` and killed an uncited ADAU1701 rationale |
| **The link mirrors gain only; delay stays independent** (2026-08-12) | Nothing planned — nobody would have thought to ask. Corrected what `linkgroup_num` means |
| **Two vendor apps display gain differently** (2026-08-11) | Reverted a wrong "correction" reached by inference from eight consistent readings |
| **Outputs 7/8 are two subwoofers in one ported box** (2026-08-10) | The entire gang model. Not discoverable from the device at all |
| **The input gain knob is hard against its stop** (2026-08-08) | Retro-validated a session's absolute levels instead of re-measuring them |
| **`dspcartunebackups.DDP` existed in the repo root** | A differential acoustic experiment, and it disproved the premise that EQ frequencies are table-quantised |
