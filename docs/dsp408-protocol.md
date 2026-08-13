# DSP-408 control protocol

**Living document.** Populated by the Milestone 0 spike. Claims here carry their evidence; anything marked *unverified* is a hypothesis, not a finding.

## Architecture

The DSP-408 is built around **Analog Devices ADAU1701 SigmaDSP** silicon — a documented, I²C-controllable part with an open-source control library for the family ([MCUdude/SigmaDSP](https://github.com/MCUdude/SigmaDSP)) and community documentation of its register and memory layout ([freeDSP](https://freedsp.github.io/)). This is the single most important fact about the unit: it is not a proprietary black box.

### Two chips, not one — **CONFIRMED**

Board photographs from a [teardown](https://cyberpithilo.web.fc2.com/audio/openit/Dayton_dsp/index.html) show **two ADAU1701JSTZ** in QFP, side by side on a DSP daughtercard (silkscreen `YDW-DBS480-DSP-CT2`, date code 1805). This is consistent with the arithmetic: the part is 2-ADC/4-DAC, so 4-in/8-out is exactly two of them.

Consequences, all structural:

- **Program space (1024 instructions) and delay RAM are per chip.** The budget is two independent pools of four channels, not one pool of eight.
- Relative delay between drivers on *different* chips is still acoustically meaningful, but it is drawn from different RAM pools — the accounting and the acoustics group channels differently.
- Two I²C addresses, and the chips must be clock-synchronised. A single **12.288 MHz** oscillator serves the card (256 × 48 kHz), which is consistent with both running from one clock domain.

> **Settled 2026-08-11: the channel-to-chip mapping is measured.** The ADAU at I²C `0x37` drives outputs 1–4; the one at `0x35` drives 5–8. Taken on a logic analyser tapping the ADAU control bus, one gain step per output, with outputs 3–6 falsified by a reverse-order re-run. `DeviceLimits` and `SimulatedDsp` now model **two per-chip pools of four**. The *sizes* of those pools remain unmeasured placeholders.

### Signal and control topology *(from teardown photographs)*

```
PC      ──USB-B──────────────┐
Phone   ──BT──► DSP-BT ──USB-A (SPI, not USB)──┤
DSP-RC  ──RJ11───────────────┤
                             ▼
                    STM32F103C8T6  ──I²C──► ADAU1701 #1 ──► outputs 1-4?
                    (64 KB flash)  ──I²C──► ADAU1701 #2 ──► outputs 5-8?
                                                ▲
                                        12.288 MHz (256 × 48 kHz)
```

**The USB-A port does not carry USB.** The teardown states it carries SPI for the Bluetooth dongle — 「接続されている信号はUSBとは異なるSPI接続のものなので普通のUSB Bluetoothドングルを挿しても全く電源供給されず使用できない」 (*the signal is SPI, not USB, so an ordinary USB Bluetooth dongle gets no power at all and cannot be used*). Dayton's own documentation corroborates that generic dongles do not work.

**PC control is a separate USB-B port** and is real USB — the manual lists "1 USB B for PC control" and "1 USB A for DSP-BT4.0 connection" as distinct connectors. The STM32F103C8T6 has a native USB 2.0 FS device peripheral, so the PC almost certainly talks to the STM32 directly.

Other identified parts: 4× NE5532 output buffers on the DSP card, input buffers and 4 relays (high-level input switching) on the mainboard (`DSP-408 180808 V1.1`).

> ### ⚠ Corrected 2026-08-08 by the operator's own board photographs
>
> Two claims below, both inherited from the third-party teardown, are wrong.
> See `docs/board-probing.md` and `Board Images/`.
>
> 1. **The MCU is a Geehy APM32F103, not an ST STM32F103C8T6.** A pin- and
>    largely register-compatible Chinese clone. SWD should still work, but
>    published STM32F103 readout-protection bypasses do **not** transfer —
>    different die, different flash controller. Treat RDP as unknown.
> 2. **There IS an 8-pin Atmel serial EEPROM on the DSP card** (`ATMLH146 /
>    2ECL CN`), beside the MCU. The paragraph immediately below says there is
>    none, and the inference that the tune must live in MCU internal flash
>    rested on that. The EEPROM's position suggests MCU config storage rather
>    than ADAU1701 self-boot — untested. **If the tune lives there it is
>    readable non-destructively**, which would yield the whole preset structure.
>
> Board silkscreen also reads `YDW-DDS480-DSP-CT2 1-XLB-089 180301`, against the
> teardown's `YDW-DBS480-DSP-CT2` — a different unit or revision, which is
> reason enough to prefer our own photographs throughout.

**No self-boot EEPROM is visible on the DSP card.** The ADAU1701s are therefore very likely programmed over I²C by the STM32 at boot, which means **the STM32's 64 KB flash contains both DSP program images and their parameter maps**, alongside the USB and SPI protocol implementations. That makes the STM32 the single highest-value target in the unit — see Tier 2.

### Published specifications

| Property | Value |
|---|---|
| Inputs | 4 RCA (≥20 kΩ), 4 high-level (180 Ω) |
| Outputs | 8 RCA (<50 Ω) |
| Sampling | 24-bit / 48 kHz |
| PEQ | 10 bands per channel, 80 total |
| Also | Crossovers, time alignment, input/output mixing |
| Control | USB-A for PC; RJ11 for the DSP-RC remote; Bluetooth via DSP-BT4.0/5.0 dongle |
| Power | 9–17 V |

## Known from the manual

Free Phase D answers, taken from the [DSP-408 user manual](https://www.daytonaudio.com/images/resources/230-500-dayton-audio-dsp-408-manual\(2\).pdf):

| Parameter | Value |
|---|---|
| Time delay range | 0 to 277 cm / **8.1471 ms**, 1 cm steps |
| Crossovers | High / Low / Band-pass; **6, 12, 18, 24 dB/oct**; Linkwitz, Butterworth, Bessel |
| PEQ | 10 bands per channel, 80 total |
| Mixing | Input/output matrix, per-output percentage of each input |
| Other | Per-channel level, mute, 180° phase invert, channel linking, presets |

Two things to carry into the implementation:

- **The manual's delay arithmetic is wrong.** It states "1 cm which equals 0.294 ms". The correct figure is 8.1471 ms ÷ 277 cm = **0.0294 ms/cm** — off by 10×. The 0.394 in/cm figure alongside it is correct, so this is an isolated typo. Do not implement the printed value.
- **The cm grid is not an integer number of samples.** 8.1471 ms at 48 kHz is ~391 samples, and 1 cm is ~1.41 samples. Either the DSP does fractional delay, or the underlying parameter is an integer sample count (0–391) and centimetres are a lossy UI convenience. **Resolved 2026-08-08: it is the integer sample count.** See below.

### Resolved: delay is an integer sample count at 48 kHz

**Measured 2026-08-08**, by reading a real vehicle tune already loaded on the
device. The Windows app's delay unit selector offers ms / cm / in; set to
**ms**, the five active output channels read:

| Channel | ms | × 48 kHz | dev. from integer samples | cm | dev. from integer cm |
|---|---|---|---|---|---|
| CH1 | 2.2083 | **105.998** | 0.002 | 75.08 | 0.08 |
| CH2 | 0.1875 | **9.000** | 0.000 | 6.38 | 0.38 |
| CH3 | 2.3958 | **114.998** | 0.002 | 81.46 | 0.46 |
| CH4 | 0.2917 | **14.002** | 0.002 | 9.92 | 0.08 |
| CH5 | 0.7708 | **36.998** | 0.002 | 26.21 | 0.21 |

Every value is an exact integer number of samples at 48 kHz, to within the
4-decimal display rounding (worst deviation 0.0016 samples). **None** is an
integer number of centimetres (worst deviation 0.46 cm). Five independent
values landing on exact integer samples by chance is not credible.

**Implications:**

- The optimizer should work in **integer samples at 48 kHz** — the device's
  native grid. Centimetres and milliseconds are both lossy display
  conveniences, so a delay requested in cm will not round-trip.
- Usable range 0–391 samples, consistent with the manual's 0–277 cm.

This was answered by *reading* an existing tune, without writing anything. The
differential acoustic measurement originally planned for this question would
not have worked anyway — see the latency jitter figures in
[hardware.md](hardware.md).

### Validated: crossovers are textbook Linkwitz-Riley

**Measured 2026-08-08.** Output 1 set to HP 500 Hz / LP 2000 Hz, Linkwitz-Riley
24 dB/oct, measured against the analytic LR4 response. LR4 is two cascaded
2nd-order Butterworth sections, so it sits **−6.02 dB at the corner**, not
−3 dB — which makes corner frequency a sharp test rather than a loose one.

| | requested | measured | error |
|---|---|---|---|
| High-pass corner | 500 Hz | 493.1 Hz | −1.39 % |
| Low-pass corner | 2000 Hz | 2007.9 Hz | +0.40 % |
| Low skirt slope | 24 dB/oct | 23.30 | |
| High skirt slope | 24 dB/oct | 26.41 | |

Residual against theory is within **±0.21 dB at every probed point from 198 Hz
to 3.2 kHz** — four octaves. Outside that window the residual grows as the
theoretical attenuation approaches the rig's noise floor, which bounds the
measurement rather than the device.

The optimizer's crossover model may therefore assume true LR4 at the requested
frequency. This doubles as a known-answer test of the measurement engine,
independent of the REW golden files and taken on live hardware.

The delay figures also imply 8 × 391 = 3128 samples of delay across the unit, which would not fit a single ADAU1701's delay RAM but comfortably fits two. Corroborating, though the photographs already settle it.

"Linkwitz/Butterworth/Bessel/Riley" in the spec list appears to be Linkwitz-Riley split across two entries; treat as three alignment families until confirmed.

## What is not known

Updated 2026-08-09. Struck entries were closed by measurement; see Findings.

**Closed:**

- ~~**Frame format** on `0xFFE2`/`0xFFE1`~~ — decoded from the APK, then validated byte-for-byte against real traffic. The transport is RFCOMM, not those characteristics.
- ~~Whether the delay parameter is samples or centimetres on the wire~~ — **integer samples at 48 kHz.**
- ~~**PEQ parameter ranges** — frequency resolution, gain and Q limits, quantization~~ — frequency is continuous at 1 Hz; bandwidth is quantized at 0.01 octave/step; gain is `raw/10 − 60`. Absolute limits still unprobed.
- ~~Whether the unit supports **readback** or is write-only~~ — **readback works**, and `DataID 119` returns a whole 296-byte channel in one reply.

**Open, and blocking:**

- ~~**Channel-to-chip mapping**~~ — **measured 2026-08-11**, see above. What remains unmeasured is the *size* of each chip's delay pool and its program space.
- **Delay RAM per chip**, and how much the vendor program has already committed.
- **Program space consumed** by the vendor's program, and therefore what is left.
- **Preset recall and store opcodes.** Absent from the capture entirely. The only device-side restore that survives everything, and currently unreachable from code.
- **Do the device EQ bands realise RBJ-shaped curves?** The optimizer fits RBJ biquads and converts bandwidth to Q by the standard peaking relation, both unmeasured. If the firmware's bandwidth definition differs, every fit is systematically wrong in a way that looks like a mediocre optimizer.
- **Frequency resolution at 20–40 Hz.** Two confirming points exist (450 Hz, 1234 Hz), both mid-band. ADAU1701 5.23 fixed-point coefficients can make effective `fc` granular even where the parameter is not.

**Open, not blocking:**

- What blocks 34 and 35 actually are (see the contradiction under Findings).
- ~~Whether byte 0 of the MISC block is `mute` or an inverted `enable`.~~
  **Settled 2026-08-09: it is `enabled`, 1 = on.**
- `DataType 3` — the input channels, entirely unobserved.
- `0x52` ERROR semantics, and when the device emits it.
- Whether the device mirrors a write to a linked channel.
- Whether 20-byte chunking is required or merely tolerated.
- Whether the ~10 Hz poll is needed to hold the link.
- Whether parameter writes are **atomic** or produce audible artifacts mid-write.
- Whether the STM32's readout protection is enabled.

## Transports vs. control surfaces

Two independent questions, easily conflated:

**Transport** — how bytes reach the unit: wired USB, Bluetooth SPP via the DSP-BT dongle, or direct I²C on the board.

**Control surface** — what those bytes mean: the vendor's command protocol, direct ADAU1701 parameter-RAM writes, or a program of our own.

The vendor protocol is probably reachable over *both* wired USB and Bluetooth. If the decompiled app shows a shared command layer beneath two transports, then transport becomes a deployment convenience rather than an architectural decision.

### Route A — vendor protocol

Decode what the vendor app speaks and reimplement it.

- **For:** non-invasive. No soldering, no warranty concerns. Over Bluetooth it needs no drivers at all — an RFCOMM socket works identically from Windows, Linux or a Pi.
- **Against:** inherits the vendor's parameter granularity and update rate. Some parameters may not be independently addressable.

### Route B — direct I²C to the ADAU1701s

Tap SDA/SCL between the bridge MCU and the DSPs; write parameter RAM directly.

- **For:** keeps vendor firmware intact. Fast live updates, bypassing whatever rate limit the vendor transport imposes. The chip's I²C conventions are public.
- **Against:** requires opening the unit, soldering, and a logic analyzer. The parameter *address map* belongs to the vendor's compiled program and must be recovered by correlation.

### Route C — our own SigmaStudio program

Treat the board as ADCs + DACs + two ADAU1701s + power.

- **For:** complete ownership of the parameter map. No reverse engineering. Freedom to allocate program space and delay RAM as this project needs rather than as the vendor chose.
- **Against:** loses vendor app compatibility. Bricking risk. Needs SigmaStudio and knowledge of the board's pin and clock configuration — including how the two chips are synchronised. Highest effort, highest ceiling.

## Toolchain

Ordered by cost and risk. Each tier happens only if the previous falls short. **Phases A–B cost nothing.**

### Tier 0 — Static analysis · $0 · no hardware

| Tool | Purpose |
|---|---|
| **jadx** / `jadx-gui` | Decompile the Android APK to readable Java. Highest-value single tool here. |
| APK via **APKPure** or `adb pull` | `leon.android.chs_ydw_dcs480_dsp_408` (v1.23). No phone needed to obtain it. |
| **Detect It Easy** | Identify the Windows app's runtime; decides the next tool. |
| **ILSpy** / **dnSpyEx** | If .NET — near-source decompilation. |
| **Ghidra** | If native (Delphi/MFC/Qt). Higher effort, still free. |
| **BlueStacks** | Run the app in demo mode to correlate UI actions against decompiled code paths. |

The app is free and **runs in demo mode without hardware**, so the complete protocol implementation ships inside the APK. This work runs while the unit is in transit.

*If the APK is obfuscated:* decompile a sibling rebrand of the same OEM platform — the **DS18 i48DSP** app, or the generic "Car DSP Remote" (`com.zddz.app.ty.cardspremote`). Different builds carry different obfuscation seeds, and shared protocol constants match across them.

> **BlueStacks cannot capture Bluetooth.** No mainstream Android emulator passes through the host radio — Windows owns it exclusively. BlueStacks is for reading and running the app, never for sniffing it. HCI capture needs a real phone.

### Tier 1 — Live capture · $0 · needs the unit

| Tool | Purpose |
|---|---|
| **USB Device Tree Viewer** | Read descriptors first: VID/PID, class, endpoints. Two minutes that tell you whether this is CDC serial, HID, or vendor class. |
| **USBPcap + Wireshark** | Capture URBs. Covers all three cases with one tool — no separate serial monitor needed. |
| **Android Bluetooth HCI snoop log** | Developer Options. Captures phone↔dongle traffic with no extra hardware. |
| **adb** (Platform Tools) | `adb bugreport` to extract the snoop log; also pulls the APK. |

### Tier 2 — Hardware probing · ~$70 · only if Tiers 0–1 fail

| Tool | Cost | Purpose |
|---|---|---|
| **ST-Link V2 clone** | ~$3 | SWD access to the STM32F103C8T6. **Try this first** — see below. |
| **OpenOCD** or **stm32flash** | $0 | Drive the ST-Link; dump flash. |
| 8-channel USB logic analyzer | ~$12 | I²C at 100–400 kHz. A generic Saleae clone suffices; a Logic 8 is not warranted. |
| **PulseView / sigrok** | $0 | I²C decoder, CSV export for scripted analysis. |
| Test hooks / micro-grabbers | ~$20 | LQFP-48 at 0.5 mm pitch cannot be grabbed pin-by-pin; use the headers below. |
| USB microscope or loupe | ~$30 | Locating test points. Chip identification is already done. |
| Soldering iron, flux, 30 AWG wire | owned | Populating headers or tapping SDA/SCL. |

**The STM32's flash is the jackpot, not an EEPROM.** No self-boot EEPROM is visible on the DSP card, so the STM32F103C8T6 almost certainly holds the ADAU1701 program images and their parameter maps in its 64 KB flash — alongside the USB and SPI protocol code. One successful SWD dump would deliver the protocol, the parameter map, and the resource figures simultaneously, replacing jadx, USBPcap, the logic analyzer and the differential-capture harness in a single step.

Whether that works turns on readout protection. Vendors of consumer gear frequently leave RDP disabled; if so, a $3 clone and OpenOCD read all 64 KB. If RDP level 1 is set, the STM32F103 has well-documented weaknesses, but treat that as a separate decision rather than an assumed continuation.

> The **earlier advice to buy a CH341A EEPROM programmer no longer applies** to this unit — there is nothing for it to read. It remains relevant only if a later teardown finds an EEPROM this one missed.

**Access looks solder-light.** The DSP card's underside carries an unpopulated **2×5 header footprint** — the standard footprint for both ARM SWD and the ADI USBi — plus two 4-pad groups with square pin-1 markers, consistent with I²C or UART breakouts. Pogo pins or a soldered pin header should reach these without touching a 0.5 mm-pitch package. Confirm what each footprint actually is with a multimeter before connecting anything.

Safety: the unit is powered during capture. Ground the analyzer to DSP ground first.

### Tier 3 — Writing and reflashing · route C only

| Tool | Cost | Purpose |
|---|---|---|
| **SigmaStudio** (ADI, Windows) | $0 | Required for route C; useful in every route for parameter number formats. |
| ADI USBi clone, or Arduino/Pi as I²C master | ~$30 | Program download, driven by MCUdude/SigmaDSP. |

### Tier 4 — Analysis code we write

The actual intellectual tool. Lives in the repo, built when there are captures to feed it:

- **Differential capture harness.** N captures, each with one parameter at one known value; parse with `pyshark` or `scapy`; diff frames to isolate which bytes encode what; fit the byte→value mapping.
- **Fixed-point decoder.** SigmaDSP parameter RAM is 28-bit. Verify the exact format against the datasheet, then test candidate 4-byte fields against it — a field that decodes to a sensible filter coefficient almost certainly is one. A correct format hypothesis collapses most of the guesswork.
- **Transport client.** `pyserial`, `pyusb` + libusb (with **Zadig** on Windows to bind WinUSB), `hidapi`, or plain RFCOMM sockets — whichever the transport turns out to be.

## Execution

**Phase A — now, while the unit ships. No hardware.**
1. Obtain and decompile the APK with jadx.
2. Locate command construction and transport code. Extract framing, opcodes, parameter encoding, address tables.
3. Triage the Windows app with DIE; decompile with the matching tool; cross-check against the APK.
4. Record everything below under **Findings**, including negative results.

**Phase B — unit arrives.**
5. Read USB descriptors; record VID/PID and class.
6. Pair the DSP-BT5.0; attempt a decoded command over RFCOMM. **Verify acoustically** — see below.
7. If Phase A was ambiguous, capture ground truth (HCI snoop log or USBPcap) and diff against predictions.

**Phase C — only if A and B fail.**
8. Open the unit; identify both DSPs and the bridge MCU under magnification.
9. Look for a self-boot EEPROM and read it before soldering.
10. Tap I²C; capture with PulseView while the vendor app changes known parameters.

**Phase D — resource figures. Required regardless of route.**
11. Confirm the chip count and the channel-to-chip mapping.
12. Determine per-pool delay RAM, program space consumed, max delay per channel, crossover types and slopes, PEQ ranges and quantization, and whether readback exists.

## Verification standard

**A decoded command is confirmed only by acoustic measurement.** Write a known PEQ band, measure, and check the filter appears at the right frequency with the right gain and Q.

The device not returning an error is **not evidence**. A unit that silently discards a malformed packet is indistinguishable from one that accepted it, and that indistinguishability is exactly how a wrong protocol model survives long enough to be built on.

Where readback exists, add a write→read→compare round trip. That is corroboration, not a substitute: it proves the unit stored what you sent, not that it interpreted it as you intended.

Once real limits are known, `SimulatedDsp` must reject exactly what the hardware rejects. Test by attempting a known-too-large allocation against both.

## Findings

### 2026-08-06 (late) — PROTOCOL DECODED from the vendor APK

Pulled the installed app off a Galaxy S10+ over wireless ADB
(`leon.android.chs_ydw_dcs480_dsp_408`, v1.23, versionCode 19) and decompiled
`base.apk` with jadx — 1747 source files, of which 274 are the app's own.

The protocol is **not obfuscated**. It is a plainly-named `datastruct` package.

#### Frame constants — `datastruct/DataStruct.java`

| Constant | Value | Meaning |
|---|---|---|
| `FRAME_STA` | **0xEE** (238) | frame start |
| `FRAME_END` | **0xAA** (170) | frame end |
| `WRITE_CMD` | **0xA1** (161) | write |
| `READ_CMD` | **0xA2** (162) | read |
| `RIGHT_ACK` | 0x51 (81) | success |
| `ERROR_ACK` | 0x52 (82) | failure |
| `DATA_ACK` | 0x53 (83) | data response |
| `CMD_LENGHT` | 16 | command frame length *(vendor's spelling)* |
| `U0DataLen` | 800 | bulk data buffer |
| `AgentHead_YDW` | 128 (0x80) | vendor head — matches the `YDW` board silkscreen |

#### Frame fields — `datastruct/Data.java`

`FrameStar`, `FrameType`, `DeviceID`, `UserID`, `DataID`, `DataType`,
`ChannelID`, `DataLen`, `CheckSum`, `FrameEnd`.

**Readback exists.** `READ_CMD` and `DATA_ACK` settle a question that was
previously open: the device is not write-only, so write→read→compare round
trips are available.

#### Per-output parameters — `datastruct/DataStruct_Output.java`

`gain`, `delay`, `mute`, `polar`, `spk_type`, `linkgroup_num`,
`h_filter`/`h_freq`/`h_level` (high-pass), `l_filter`/`l_freq`/`l_level`
(low-pass), `eq_mode`, `allPassQ`, compressor (`threshold`, `attackTime`,
`releaseTime`), a 16-entry input-mix vector `IN1_Vol`…`IN16_Vol`, an 8-byte
`name`, and **`DataStruct_EQ[31]`**.

Each EQ band is `{type, freq, level, bw, shf_db}`.

The structures are sized 16-in/16-out throughout. This is a generic OEM
platform and the DSP-408 is a 4×8 subset of it — consistent with the
`YDW`/`DCS480` naming on both the board and the package.

#### ⚠ Frequencies are quantized to tables, not continuous

`Define.java` carries two fixed tables:

* **`EQ_FREQ`** — 31 entries, standard 1/3-octave: 20, 25, 32, 40, 50, 63, 80,
  100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1k, 1.25k, 1.6k, 2k, 2.5k,
  3.15k, 4k, 5k, 6.3k, 8k, 10k, 12.5k, 16k, 20k Hz.
* **`XOver_FREQ`** — 51 entries from 20 Hz to 20 kHz.

**This directly constrains the optimizer.** `tuner.optimize.biquad` currently
fits centre frequency as a continuous parameter. It cannot: a PEQ band can only
sit on one of 31 fixed frequencies, and a crossover on one of 51. Fitting
continuously and rounding afterwards gives a different — and worse — answer
than fitting on the discrete set. This needs to change before M4.

#### Reproducing

```bash
adb pair <ip>:<port> <code> && adb connect <ip>:<port>
adb shell pm path leon.android.chs_ydw_dcs480_dsp_408   # split APK; take base.apk
adb pull <base.apk path> base.apk
jadx -d src --no-res base.apk
```

Everything above is in `sources/leon/android/chs_ydw_dcs480_dsp_408/datastruct/`.

#### Wire format — COMPLETE

From `operation/DataOptUtil.java::SendDataToDevice()`. All multi-byte fields
are **little-endian**.

```
offset  size  field
  0..2    3   0x80 0x80 0x80      vendor head (AgentHead_YDW = 128)
     3    1   0xEE                frame start (Define.SEFFFILE_EncryptByte)
     4    1   FrameType           0xA1 = write, 0xA2 = read
     5    1   DeviceID            observed 1
     6    1   UserID              observed 0
     7    1   DataType            9 = system; 3 = input channel; ...
     8    1   ChannelID           selects the sub-block being written
     9    1   DataID              index within the sub-block (e.g. EQ band)
    10    1   BluetoothDeviceID   from MacCfg
    11    1   PcCustom            observed 0
 12..13    2   DataLen             uint16 LE, payload length
 14..      N   payload
14+N      1   CheckSum
15+N      1   0xAA                frame end

total length = DataLen + 16      (hence CMD_LENGHT = 16 of overhead)
```

**Checksum** — XOR of every byte from offset 4 through 13+DataLen inclusive:

```python
cs = frame[4]
for i in range(datalen + 9):
    cs ^= frame[5 + i]
```

A computed checksum of **zero is rejected by the app itself** as an error
condition, so zero is not a legal checksum value.

**Payload example — one EQ band** (DataType 3, DataID = band index):

```
14..15  freq    uint16 LE   index into EQ_FREQ, not Hz
16..17  level   uint16 LE
18..19  bw      uint16 LE
   20   shf_db  uint8
   21   type    uint8
```

For a read (`FrameType = 0xA2`) the app forces `DataLen = 0`.

#### Output channels — `DataType = 4`

`ChannelID` selects the output. `DataID` selects which block the 8-byte
payload carries. From the `DataType == 4` branch of `SendDataToDevice`.

| DataID | Block | Payload (little-endian) |
|---|---|---|
| **0–30** | PEQ band *n* | `freq` u16, `level` u16, `bw` u16, `shf_db` u8, `type` u8 |
| **31** | misc | `mute` u8, `polar` u8, `gain` u16, `delay` u16, `eq_mode` u8, `spk_type` u8 |
| **32** | crossover | `h_freq` u16, `h_filter` u8, `h_level` u8, `l_freq` u16, `l_filter` u8, `l_level` u8 |
| **33** | mix in 1–8 | eight u8 levels |
| **34** | mix in 9–16 | eight u8 levels |
| **35** | dynamics | `allPassQ` u16, `attackTime` u16, `releaseTime` u16, `threshold` u8, `linkgroup_num` u8 |
| **36** | name | eight ASCII bytes |
| — | whole channel | `DataLen = 296` writes the entire channel in one frame |

Two traps in this layout:

* **EQ bands and parameter blocks share the DataID space.** Bands occupy
  0–30 and `MISC` is 31, so an off-by-one on the band index writes EQ data
  into the misc block — changing mute, gain and delay instead. Pinned by a
  test asserting `OutputBlock.MISC == OUTPUT_EQ_BANDS`.
* **The crossover block is asymmetric.** Frequencies are 16-bit but filter
  type and slope are single bytes, so it is not a uniform array of u16s.
  Assuming otherwise shifts every field after the first.

Implemented in `tuner.dsp.protocol`; `tests/test_protocol_output.py` pins the
byte layout of each block, because a transposed field writes a plausible value
into the wrong parameter and the device accepts it without complaint.

#### ⚠ Dangerous opcodes — do not send

`DataType = 9` (system) with these ChannelID values triggers destructive
operations. Each sends an 8-byte payload from a named constant:

| ChannelID | Constant | Effect |
|---|---|---|
| **96** | `RESET_MCU` | resets the microcontroller |
| **97** | `TRANSMITTAL` | unclear; likely a transfer/bootloader trigger |
| **98** | `RESET_GROUP_DATA` | wipes stored group/preset data |

This is exactly the class of command that made blind fuzzing unacceptable.
Any client implementation should refuse to emit these unless explicitly and
separately authorized.

### Measured 2026-08-08: the transport is RFCOMM, not BLE

**Source:** an Android HCI snoop log of the vendor app driving the device,
`captures/btsnoop_hci.log`, decoded by `tuner.dsp.btsnoop` /
`tools/btsnoop_extract.py`. 16 928 HCI packets, 5 834 protocol frames.

| Layer | What the capture shows |
|---|---|
| L2CAP | dynamically-allocated CIDs `0x0049` / `0x0480`, ~11 800 packets |
| | fixed CID `0x0004` (BLE ATT): **5 packets**, none carrying our protocol |
| RFCOMM | UIH frames, credit-based flow control, 20-byte payloads |
| Above it | our `80 80 80 EE … AA` frames, unmodified |

The earlier conclusion that "control is BLE-only" came from a GATT scan finding
a writable `0xFFE2` and an SDP scan finding no SPP. **A scan says what a device
offers; a capture says what the software uses.** The capture wins.

**The app talks on RFCOMM DLCI 2, i.e. SPP server channel 1** — 11 784 frames
on that DLCI across both directions, against eight multiplexer-control frames
on DLCI 0. Discover the channel by SDP at connect time rather than hard-coding
it, but 1 is what to expect and a useful sanity check if discovery returns
something surprising.

**Consequences for M3:** an RFCOMM socket, not a GATT client — a COM port on
Windows, `BTPROTO_RFCOMM` on Linux. `bleak` is not needed. Whether the BLE path
also works is now an open question rather than the plan, and worth testing on
the spare unit if one is ever acquired.

### Measured 2026-08-09: byte 0 of the MISC block is an inverted `enable`

**Source:** `eq_channel1_no_mute.DDP` vs `eq_channel1_mute.DDP`, an operator A/B
in the Windows vendor app. Output 1 muted, nothing else touched.

```
unmuted  01 01 f4 01 90 00 00 01
muted    00 01 f4 01 90 00 00 01
```

**Exactly one block in the entire 553-block file changed**, and within it one
byte: `1 -> 0`. So the field is `enabled`, and the sense is the opposite of the
`mute` name the decompiled app gives it.

Two consequences worth stating separately:

* **`gain_raw` is unchanged at 500.** Muting is a real, separate control, not a
  gain zeroing, so a backend can set one without disturbing the other. That was
  an explicit open question before this A/B.
* **The prediction that prompted the experiment was right.** The field reads 1
  on 111 of the 112 channel-records in the repository; the single exception is
  OUT7 in `dspcartunebackups.DDP`, a channel that was switched off. If it were a
  mute flag every *active* channel would read 0.

`tuner.dsp.protocol.OutputMisc.mute` is renamed `enabled`, and `Dsp408Spp` will
now write it. Pinned by `tests/test_bulk_record.py::TestMuteIsAnInvertedEnable`,
which also re-checks the 111/1 survey so the basis of the inference cannot
silently rot.

This is the fourth time the diff-two-backups loop has answered a question that
had a harder experiment planned for it, and the cheapest: two saves and a
`ddp_dump.py` invocation.

### Measured 2026-08-09: the session layer

The 2026-08-08 pass established the frame *grammar*. This pass establishes the
*dialogue* — the thing a backend actually has to implement. Same capture, no new
hardware, no bytes written. Every figure below was re-derived independently
before being recorded here.

#### Framing: everything moves in 20-byte chunks

| Direction | Bytes | RFCOMM frames | Sizes |
|---|---|---|---|
| host → device | 58 780 | 2 939 | **all exactly 20** |
| device → host | 118 400 | 5 894 | 5 888 × 20, 5 × 120, 1 × 40 (batched) |

**All 2 918 host protocol frames start on a 20-byte boundary, and every
inter-frame gap is `0x00`** (2 917 gaps, verified). A 16-byte read frame is
followed by 4 pad bytes; a 24-byte write occupies two chunks with 16 bytes of
padding; a 312-byte bulk reply takes 16 chunks with 8 bytes of padding.

The likely cause is the app sharing one send routine with its BLE path, sized
for BLE's 20-byte default ATT payload. **Whether the device requires the
chunking or merely tolerates it is unknown** — only one side of that experiment
is visible. Replicate it anyway: replication is free, and tolerance is
unconfirmed.

A reader must therefore treat the link as a byte stream, resync on the
`80 80 80 EE` preamble, and tolerate runs of `0x00` between frames.

#### Strict lock-step, one outstanding request

Splitting the 5 834 frames into alternating host/device positions gives
**exactly one violation**, and it is the final frame — an unanswered poll during
link death. 2 916 complete transactions.

| Request | Reply | Count |
|---|---|---|
| `0xA2` READ | `0x53` — carries the data | 2 895 |
| `0xA1` WRITE (8-byte payload) | `0x51` — **zero-length ack** | 21 |
| — | `0x52` ERROR | **never observed** |

**The reply echoes the request header bit-for-bit** — `device_id`, `user_id`,
`data_type`, `channel_id`, `data_id`, `bluetooth_device_id`, `pc_custom`, with
**0 mismatches in 2 916 pairs**. Only `frame_type` and the payload differ. So a
backend can match replies on the echoed tuple rather than on ordering, which is
what makes a stale reply from a timed-out transaction distinguishable from the
one it is waiting for.

Latency, request → reply, in milliseconds:

| Class | n | min | median | max |
|---|---|---|---|---|
| Writes | 21 | 50.9 | **85.0** | 354.1 |
| Reads | 2 895 | 23.0 | **47.3** | 339.1 |

Writes are systematically slower than reads — plausibly the non-volatile store
committing. **Set a reply timeout of ~1 s**, roughly 3× the observed maximum.

**Writes are never pipelined.** Within every burst the pattern is strictly
write, ack, write, ack. The app has exactly one request outstanding at all
times, and so should we.

#### Pacing is reply-driven, not timer-driven

The poll loop runs 2 866 requests over 286.0 s = **10.02 req/s**. Request-to-
request period: min 38.8 ms, median 66.0 ms. But turnaround — reply to next
request — has a minimum of **2.1 ms** and a median of 17.7 ms, so the 38.8 ms
floor is the *sum* of the device's ~23 ms minimum latency and a short host
delay, not an enforced rate limit.

Recommended backend policy: one outstanding request, ≥20 ms idle after a reply,
sustained rate capped at ~10/s. That is what the app achieves and it is
demonstrably sustainable for 286 s.

#### The connect ritual — 31 transactions, 5.8 s, no authentication

There is no negotiation and no handshake in the security sense, but there is an
unmistakable fixed enumeration script. `user_id` is **overloaded as an index**.

| Order | Request | Reply |
|---|---|---|
| 1 | `dt9 / ch4` | `MYDW-AV1.06` — firmware/model string |
| 2 | `dt9 / ch19` | `83 83 83 83 d1 c7 bb bb bb bb` — unidentified |
| 3 | `dt9 / ch2` | `01 00 01 00 00 00 00 00` |
| 4 | `dt9 / ch5` | `2e 00 00 32 00 32 01 00` |
| 5 | `dt9 / ch6` | `03 09 04 0a 0f 12 16 17` |
| 6–7 | `dt9 / ch7`, `ch8` | **empty payloads** |
| 8 | `dt9 / ch52` | `04` — **the current preset slot** |
| 9–23 | `dt9 / ch0`, `user_id` 1…15 | the 15 **preset name** strings |
| 24–31 | `dt4 / ch0…7`, `data_id 119` | **296 bytes each** — the whole channel |

Preset names as read: 1 `re-timed`, 2 `rockkkkkk`, 3 `- bass`, 4 `lbass`,
5 `test`, 6 `basssss++++`, 7–15 all `lbass`.

Replicate this sequence exactly. If any of it drives a device-side state
machine we get that for free, and any deviation from the observed order is
unvalidated. 5.8 s is cheap. Name the unknown channels `UNKNOWN_2`, `UNKNOWN_5`
and so on rather than inventing meanings for them.

#### `DataID 119` — the bulk channel read, and the snapshot primitive

`DataType 4 / ChannelID 0–7 / DataID 119 (0x77)` returns a **296-byte** payload
holding the complete channel: 37 blocks of 8 bytes, at `offset = data_id × 8`.

| Offset | Block |
|---|---|
| 0–247 | EQ bands 0–30 |
| 248 | MISC (`data_id` 31) |
| 256 | XOVER (32) |
| 264 | MIX (33) |
| 272 | block 34 — **see the contradiction below** |
| 280 | DYNAMICS (35) |
| 288 | NAME (36) |

Two independent confirmations of the layout:

1. **The app's own writes.** Its first write to band 3 carries `freq 2514,
   level 480` — exactly what sits at offset 24 of channel 0's record, with only
   the bandwidth changed. It could only have got those values by reading them
   from there. Same for band 2. This pins the offset mapping *and*
   read-modify-write to real traffic.
2. **The vendor app's `.DDP` export.** All eight 296-byte records are
   **byte-identical to the output section of three backups in the repository**
   (`dspcartunebackups_Channel4_preset.DDP`, `eq_1_baseline.DDP`,
   `eq_3_bypass_off.DDP`) — 2 368 bytes, zero differing bytes, produced by two
   paths that share no code. Backups saved in other tune states differ, as they
   should.

Pinned by `tests/test_bulk_record.py`.

**`DataID 119` is not in the `OutputBlock` enum and must never become a write
opcode.** A bulk *write* was never observed; `OUTPUT_BULK_LEN = 296` in
`protocol.py` is an APK reading, not something the device has been seen to
accept.

#### ⚠ Blocks 34 and 35 contradict the decompiled app

`protocol.OutputBlock` calls `DataID 34` `MIX_IN_9_16`. **The device's own
readback disagrees**, and so does `ddp.py`, which has always called it
"dynamics A". The two modules have disagreed since both were written.

| Channel | Block 33 (mix) | Block 34 | Block 35 (dynamics) |
|---|---|---|---|
| 0 | `50 00 50 00 00 00 00 00` | `a4 01 38 00 f4 01 00 00` | `a4 01 38 00 f4 01 00 00` |
| 1 | `00 50 00 50 00 00 00 00` | `a4 01 38 00 f4 01 00 00` | `a4 01 38 00 f4 01 00 00` |
| 6, 7 | `5a 5a 5a 5a 00 00 00 00` | `a4 01 38 00 f4 01 00 00` | `a4 01 38 00 f4 01 00 **01**` |

Block 33 is mix-shaped: one byte per input, channel 0 fed from inputs 1 and 3,
channel 1 from 2 and 4 — a stereo pair. Block 34 is identical on all eight
channels and decodes as `OutputDynamics(all_pass_q=420, attack=56, release=500)`.

**Nor is 34 simply a copy of 35.** They match on channels 0–5 and differ on 6
and 7 in byte 7 alone — the `linkgroup_num`, set on exactly that pair. So 34
carries the same fields as 35 without the link group.

The enum is **not** renamed: "not mix" is far better evidenced than any
particular replacement, and renaming would assert one. Instead both blocks are
listed in `protocol.UNVERIFIED_OUTPUT_BLOCKS` and **excluded from every write
path**. Writing 34 would send bytes to an opcode whose destination is
unverified, on a device with no undo.

#### The poll is a keepalive carrying no information

`dt9 / ch3 / did0`, ~10 Hz, 2 864 replies, **all byte-identical**:
`00 ×14` then `01`. Every one of the 15 byte positions has exactly one distinct
value across the whole capture. Almost certainly a metering/status block —
fourteen zeros because the bench had no signal, the trailing `01` a liveness
flag.

**Whether the device drops the link without it is unknown.** The app polls
continuously regardless of user interaction, so no idle period in this capture
tests the question. Answering it needs *our* backend to connect, send nothing,
and time the drop.

#### What the capture cannot answer

State these as gaps rather than designing around a guess:

- **Preset recall and store.** No preset action appears anywhere. `dt9/ch52`
  (current slot) and `dt9/ch0 user_id N` (names) are read once at connect and
  never again, and there is **no host write with `DataType 9` at all**. Since
  preset recall is the only device-side restore that survives everything, this
  is the most valuable missing opcode.
- **`DataType 3`, the input channels** — zero frames, either direction.
- **`0x52` ERROR semantics.** All 21 writes succeeded, so the failure path has
  never been seen.
- **Writes to `channel_id` 1–7.** Every write in the session targeted channel 0.
  Reads covered all eight, so channel id is manifestly an output selector, but
  the write path is an extrapolation.
- **Whether the device mirrors a write to a linked channel.** OUT7/8 carry
  `linkgroup_num = 1` and were never written to in this capture.
- **Whether 20-byte chunking is required or merely tolerated.**
- **Whether `bluetooth_device_id` matters** (below).

#### `bluetooth_device_id` is 4 on the wire and 0 in our code

The value is **4 in all 5 834 frames, both directions** — as are `device_id = 1`
and `pc_custom = 0`. `Frame`'s default for it is **0** (`protocol.py`), and the
field is inside the checksum, so a frame we construct differs from one the app
sends by two bytes.

It is almost certainly a host-side index into the app's paired-device list — an
artifact of the operator's phone rather than a device identity — which is why
the default is not simply changed to 4: that would encode "the fourth paired
slot" as a protocol constant. The plan is a named constant stamped in one place
by the session layer, with the value asserted on transmit and its echo checked
on receive. Find out whether the device cares during a **read**, never a write.

#### Integrity, and the disconnect

`80 80 80 EE` occurs exactly 5 834 times in the file, equalling the number of
successfully decoded frames: **zero malformed frames, zero checksum failures,
zero frame-level retransmissions.** No unsolicited device traffic — the device
never speaks first.

The session ends badly, which is itself useful: the device stopped answering
mid-poll, the app retried the identical request once after 3.2 s, then sent
RFCOMM `DISC` and got no `UA`. **There is no application-layer goodbye frame**,
and the app's failure policy is one retry, then close.

### Measured: every parameter write is a whole 8-byte block

The capture answers the write-scope question the OUT7/8 incident raised.

Seven deliberate UI changes produced **21 writes**, all `DataType 4`
(OUTPUT_CHANNEL), all `channel_id 0`, all with an 8-byte payload, each
acknowledged by an `0x51` response:

| Action | Writes | What was sent |
|---|---|---|
| Channel gain, three settings | 8 | `MISC` block: gain 490, 480, 490, 500, 510, 520, 510, 500 |
| Low-pass to 1234 Hz | 1 | `XOVER` block: hp 450 unchanged, lp 1234 |
| PEQ band 3, Q then gain | 5 | whole band: freq 2514 unchanged, bw 0 → 49 → 108 → 110, level 480 → 600 |
| PEQ band 2, Q, gain, freq | 7 | whole band: bw 0 → 79 → 106, level 600 → 657 → 689, freq 486 → 2245 → 12699 |

Two findings, both load-bearing for the backend:

1. **A write carries the entire block, never a single field.** Changing a
   band's Q rewrites its frequency and level too; changing a channel's gain
   rewrites its delay, polarity and speaker type. **So a backend must
   read-modify-write**, or it will silently revert every other field in the
   block to whatever it last believed.

   > **Hypothesis, not measurement.** This was previously written as though it
   > also explained how a linked pair both move from one slider drag. It does
   > not: whole-block writes explain nothing about *linking*, and the capture
   > cannot settle whether the device mirrors a write to a linked channel or
   > the app simply sends two. Every write in it went to `channel_id 0`, and
   > OUT7/8 — the only pair with `linkgroup_num = 1` — were never touched.
   > Until a capture shows a link toggle, **no write goes to a linked channel.**
2. **The app streams intermediate values.** A slider drag emits a frame per
   position — eight writes for three settled gain values. Harmless for the app;
   for us it means writes are cheap and the device tolerates a high rate, but
   also that a capture's frame count does not equal the operator's action count.

### Validated: the encoder against real device traffic

Every prediction made from the measured scalings, before the capture was
opened, appears verbatim in it:

| Predicted | Bytes | Found |
|---|---|---|
| gain_raw 480 / 500 / 520 | `E0 01` / `F4 01` / `08 02` | ✓ |
| low-pass 1234 Hz | `D2 04` | ✓ |
| PEQ level 689 (+8.9 dB) | `B1 02` | ✓ |
| PEQ freq 12699 Hz | `9B 31` | ✓ |
| PEQ `bw_raw` 106 (Q 1.268) | `6A 00` | ✓ |

`bw_raw` 106 is worth noting: 107 is what you get by feeding the app's
*displayed* Q of 1.268 back through `bw_raw_for_q`, because the displayed value
is already rounded and the ceil rule then overshoots by one step. The capture
confirms 106. **A backend must compute `bw_raw` from exact bandwidth, never
from a rounded Q.**

This is the golden-frame validation the project had no other way to obtain.

### Settled: parameter scaling

**Measured 2026-08-08** against the vendor app's display, using read-from-device
backups. Implemented in `tuner.dsp.protocol`, pinned by `tests/test_protocol.py`.

| Field | Encoding | Evidence |
|---|---|---|
| `delay_raw` | **integer samples at 48 kHz** | Five channels against the app's ms display, to four decimals |
| `gain_raw` | **dB = raw/10 − 60** | Exact on all eight channels, raw 410/433/470/480/500 |
| EQ `level` | **same encoding**, 600 = 0 dB | 600 → 0.0 dB, 540 → −6.0 dB, 589 → −1.1 dB, 613 → +1.3 dB |
| EQ `bw` | **octaves = (raw + 5)/100** | Five (raw, Q) pairs spanning Q 0.99–4.97 |

Output gain and PEQ level sharing one origin at 600 is what made that encoding
believable rather than a curve fit through four points.

**Gain is confirmed end to end, not just against the display.** OUT5 was stepped
from `gain_raw` 470 to 410 — a requested 6.00 dB cut — and re-measured:

| `gain_raw` | app shows | measured passband offset |
|---|---|---|
| 470 | −13.0 dB | −5.86, −5.97, −5.91 dB (three runs, 0.11 dB spread) |
| 410 | −19.0 dB | −11.92 dB |

Measured change **−6.01 dB** against −6.00 requested, an error of 0.007 dB
inside a 0.11 dB run-to-run spread. The crossover corner held at 449.5 Hz
across the gain change, which is the control. The chain raw value → app display
→ actual level is closed.

#### Settled 2026-08-09: the device runs RBJ peaking sections, half-gain convention

**The premise the whole optimizer rests on, and nobody had ever asked the
hardware.** `optimize/biquad.py` fits RBJ peaking biquads and
`protocol.q_from_bw_raw` converts `bw_raw` → octaves → Q by the standard
half-gain relation. That relation was verified against the vendor app's
*display* on five points and had never been checked against sound.

Differential measurement on OUT1, electrical loopback, −18 dBFS stimulus, band
at 1000 Hz, fitted over 62–16 000 Hz at 400 log-spaced points:

| `bw_raw` | oct | Q requested | **Q measured** | **half-gain err** | −3 dB err | rms | `.DDP` |
|---|---|---|---|---|---|---|---|
| 25 | 0.30 | 4.800 | 4.812 | **−0.6 %** | −46.7 % | 0.130 dB | (inferred, see below) |
| 65 | 0.70 | 2.041 | 2.028 | **+0.3 %** | −45.9 % | 0.110 dB | `d1_bw65_p12.DDP` |
| 134 | 1.39 | 0.999 | 0.994 | **+0.0 %** | −45.0 % | 0.144 dB | `d1_bw134_p12.DDP` |
| 65, **cut** | 0.70 | 2.041 | 2.051 | **−0.8 %** | −46.7 % | 0.141 dB | `d1_bw65_m12_corrected.DDP` |

**The half-gain error stays inside ±0.8 % while the bandwidth spans 4.6×; the
−3 dB reading sits pinned near −46 % and never moves toward agreement.** The two
conventions separate from 0.14 octaves at `bw_raw` 25 to 0.51 at 134, so a fit
merely accommodating whichever hypothesis it was handed could not track one and
not the other across that range. `q_from_bw_raw` and `biquad.py` stand.

The cut is 1.1 % from the boost in Q — inside the run-to-run spread — so the
implementation is **symmetric**, which some are not.

##### Why +12 dB and not +6

A peaking filter's half-gain points sit at `G/2` and its −3 dB points at `G−3`.
Those are **equal at G = 6**, so at +6 dB the two conventions predict the same
curve and the experiment is null by construction. An earlier draft of the run
sheet said +6 dB. Caught before the bench trip, not after.

##### Supporting measurements taken the same session

* **Repeatability.** The `bw_raw` 65 boost was measured twice: Q 2.033 / 2.028,
  centre 1000.7 / 1000.2 Hz, gain +11.97 / +11.98 dB.
* **No internal clipping.** The +12 dB is applied inside the DSP, downstream of
  everything `tuner.safety` can see, so the same band was re-measured 6 dB
  quieter: Q 2.025 (0.15 % from the −18 dBFS run), gain +11.99 dB. Had the DSP
  been clipping, the fitted gain would have come back short with a raised
  residual — which reads as *the model is wrong* when the truth is *the level is
  wrong*. Those two are indistinguishable in the output and lead to opposite
  actions.
* **Flat-path level-linearity, for free.** That run's fitted offset came to
  −6.01 dB against a −6.00 dB prediction.
* **`bw_raw` 25 was identified by the fit, not by a file.** The `.DDP` was not
  saved for that run. The measured Q of 4.812 sits 0.25 % from `q_from_bw_raw(25)`
  and 3.1 % / 3.6 % from its neighbours 24 and 26, which resolves the stored
  integer to better than one step. Weaker than the file and recorded as such;
  the trick also degrades at wide bandwidths, where steps crowd together in Q.
* **The readback discipline caught a real error.** Run 5's first save changed
  only `bw_raw`; the gain was still +12 dB. Measuring it would have recorded a
  boost as a cut and entered "the device is symmetric" into the record with no
  way to notice afterwards.

#### Settled 2026-08-09: `fc` stays continuous at 25 Hz

Every frequency point on record was mid-band — 450, 1234 and 1000 Hz. Near DC a
peaking biquad's poles crowd toward `z = 1`, so ADAU1701 5.23 fixed-point
coefficients could quantize the *effective* centre even though the stored
parameter is a free integer. Two points 2 Hz apart settle it: coarse enough that
any plausible quantization step collapses both onto one centre, fine enough that
a continuous parameter separates them.

| Set | Fitted | Error | rms | `.DDP` |
|---|---|---|---|---|
| 25 Hz | **24.9 Hz** | −0.43 % | 0.065 dB | `d1_lf25.DDP` |
| 27 Hz | **26.9 Hz** | −0.36 % | 0.073 dB | `d1_lf27.DDP` |

**Separated by exactly 2.0 Hz.** `fc` is continuous down here too, so
`optimize/biquad.py` may search low frequencies continuously with no
special-casing. The A/B changed one field — `freq_hz: 25 → 27`, confirmed by
`.DDP` diff — and nothing else.

The half-gain convention also holds at 25 Hz (+0.4 %, Q 2.032), two and a half
octaves below anywhere it had been checked.

Both runs sit 0.4 % low. That is a **common offset, not a resolution limit**:
0.1 Hz, the same in both, cancelling exactly in the difference the test turns
on. Recorded as observed, not explained.

> **One caveat on these two runs.** The out-of-band residual guard masks
> `f < freq/8` and `f > freq*8`, which at 25 Hz means below 3.1 Hz or above
> 200 Hz — and 200 Hz is the top of the fit range. Exactly one point qualified,
> so median, 95th percentile and max were all the same number and the guard was
> effectively inactive. The two runs cross-check each other instead. If more LF
> work is done, widen the fit range above `8 × fc` so the guard has something to
> stand on.

#### Bandwidth, not Q

The device stores **bandwidth in octaves**, offset so `bw_raw` 0 is 0.05
octaves and each step is 0.01 octave. The app displays Q, derived by the
standard peaking-filter relation `Q = sqrt(2^N)/(2^N − 1)`:

| `bw_raw` | octaves | Q shown |
|---|---|---|
| 24 | 0.29 | 4.966 |
| 43 | 0.48 | 2.992 |
| 52 (default) | 0.57 | 2.515 |
| 90 | 0.95 | 1.492 |
| 134 | 1.39 | 0.999 |

**Typing a Q into the app snaps it**, because the underlying store is an
integer. Observed: 1 → 0.99, 3 → 2.992, 1.5 → 1.492. Every snap moved Q *down*,
which is the signature of rounding bandwidth **up** — the device errs wide
rather than narrow. Q = 1.5 is the case that proves the rule: it stored
`bw_raw` 90, where ordinary rounding would have given 89.

**The optimizer must search bandwidth, not Q.** In bandwidth the grid is
uniform; in Q it is not remotely so. One raw step is worth 0.60 in Q near
Q = 9.6 and 0.002 near Q = 0.5 — a factor of 300. A fit that treats Q as
continuous and rounds afterwards will behave completely differently at the two
ends of the range.

Note the contrast with frequency, which is *not* quantized in any way that
matters: 1 Hz steps over a u16. Two parameters in the same 8-byte block, one
effectively continuous and one coarsely discrete.

### Settled: frequencies are continuous, not quantized

**Measured 2026-08-08 on OUT5, whose 450 Hz low-pass came from the tune and was
never touched.** Nearest table entry is 420 Hz, 6.7 % away.

| Evidence | Result |
|---|---|
| Three tones at 300 / 1000 / 3000 Hz | fc = 450 fits to **0.05 dB rms**; fc = 420 → 1.65 dB, fc = 486 → 1.85 dB |
| Swept fit, 200–2000 Hz, 400 points | corner **447.0 Hz** (−0.67 %) |
| Swept fit, 200–1600 Hz, 400 points | corner **449.4 Hz** (−0.14 %), rms residual 0.241 dB |

Against "snapped to 420" the same fit is +6.99 % out. **The device honours the
frequency it is given.** `EQ_FREQ_TABLE_HZ` and `XOVER_FREQ_TABLE_HZ` are the
app's default band layout, not device constraints.

Corroborating, from the app itself: a high-pass typed as **1234 Hz** was
accepted and stored verbatim — the field is free entry, not a picker.

Consequences:

- `tuner.optimize.biquad` **may fit centre frequency continuously.** The
  discrete-frequency rewrite that was blocking M4 is not needed.
- `nearest_eq_index` / `nearest_xover_index` are not quantizers and must not be
  used as such. They describe where the app parks default bands.
- Resolution is 1 Hz over a u16, so the practical range is 20 Hz–20 kHz with
  1 Hz granularity. Confirm the upper bound before relying on it.

Method: `tools/bench_crossover.py`, which fits LR4 corners by least squares
against `x^4/(1+x^4)`. Self-tested on synthetic data first — seeded at 450 it
recovers a true 420 Hz corner as 420.0, so it cannot merely confirm its seed.

#### ⚠ The "index, not Hz" reading is contradicted by the operator's own tune

The payload note above says `freq` is an index into `EQ_FREQ`. The vendor
backup file says otherwise, and the backup is the stronger evidence.

`dspcartunebackups.DDP` — saved from `DSP-408-Windows-V1.24` — stores the same
8-byte parameter blocks this protocol carries, and stores frequencies **in Hz**.
The tune in it uses 25 PEQ centre frequencies and three crossover corners that
do not appear anywhere in either table:

| | in the tune | nearest table entry | separation |
|---|---|---|---|
| Crossover | 450 Hz | 420 | 6.7 % |
| Crossover | 2500 Hz | 2594 | 3.8 % |
| Crossover | 55 Hz | 57 | 3.6 % |
| PEQ | 8619, 5341, 2514, 4891, 1899 Hz … | — | up to 13 % |

Values like 8619 Hz and 5341 Hz are not choices from a dropdown.

Two readings were possible, and **the operator has eliminated one of them**:

1. **The tables are only the app's default band layout.** The 31 EQ entries are
   exactly where the app parks the 31 bands before you move them — the tune
   confirms this, with untouched bands sitting on table values and moved bands
   sitting anywhere. Frequency is then continuous at 1 Hz resolution and
   `tuner.optimize.biquad` may fit it continuously.
2. ~~**The app stores Hz and quantizes on send.**~~ **Eliminated.** The operator
   states (2026-08-08) that this backup was produced with the app's
   *read-from-device* function, not saved from app-side project state. If the
   app quantized on send, the device would be holding table values and the
   readback would have shown them. It shows 8619 Hz, 5341 Hz, 450 Hz. The app
   therefore does not quantize on send, and the device *stores* off-table
   frequencies.

Provenance of that elimination: **an operator statement, not a file or a
measurement** — the weakest of the three, per the evidence-grading rule in
CLAUDE.md. It is enough to stop building a discrete-frequency optimizer; it is
not enough to declare the question closed.

**The app's own defaults do not fit the table either.** Resetting output 1 to
flat (2026-08-08, captured in `dspcartunebackups_flat_channel_1_diff.DDP`)
reveals the layout the app parks the 31 bands on:

| Bands | Layout | Frequencies |
|---|---|---|
| 0–9 | 10-band octave | 31, 65, 125, 250, 500, 1000, 2000, 4000, 8000, 16000 |
| 10–30 | 21-band ⅓-octave | 200 … 20000 |

Bands 10–30 are exactly `EQ_FREQ_TABLE_HZ[10:31]`, which is presumably why the
table was read as a constraint. But **bands 0 and 1 sit at 31 Hz and 65 Hz, and
the table contains 32 and 63.** A quantization grid that cannot express the
app's own default band positions is not a quantization grid. The table is a
default-population list.

That leaves one possibility open, and it is the reason to still measure: the
device could store frequency faithfully and quantize *internally* when it
computes biquad coefficients. There is no reason to burn a u16 on a value you
intend to round to one of 31, and the evidence above makes it unlikely — but it
is exactly the class of plausible-wrong assumption this project measures rather
than reasons about. The corner test below settles it in one sweep.

**`bw_raw` default is 52.** Every band reset to flat came back with `bw_raw`
52, which pins the default but not the unit — 5.2 as a Q or 0.52 as an octave
bandwidth are both still live. See `docs/STATE.md`.

**Last night's crossover measurement cannot separate them.** HP was requested at
500 Hz and measured 493.1 Hz. 500 is *not* in the crossover table; the nearest
entry is 486. So the measurement is −1.38 % against "honored" and +1.46 %
against "snapped to 486" — almost exactly equidistant. The LP leg is no help
either: 2000 Hz is in the table, so both readings predict the same answer.

**The discriminating test** is to probe at a log-midpoint between two table
entries, where "honored" and "snapped" predict answers ~7.5 % apart against a
method that resolves corner frequency to ~1.4 %:

| Probe | Between | If honored | If snapped |
|---|---|---|---|
| **452 Hz** (xover) | 420 / 486 | 452 | 420 or 486 |
| **1414 Hz** (PEQ) | 1250 / 1600 | 1414 | 1250 or 1600 |

The tune already sitting in the device uses 450 Hz on OUT1/OUT2, which is within
0.4 % of that ideal probe point — so the first half of this test needs **no
write at all**, only a measurement of the tune as it stands.

#### Persistence: parameter writes are immediately non-volatile

**Measured 2026-08-08 by power-cycle experiment.** An earlier version of this
section predicted the opposite and predicted a commit opcode. That was wrong,
and the correction matters more than the original claim did.

The experiment: load preset 4, bypass the EQ on all channels, **make no save to
the device**, disconnect USB, pull power, repower, reconnect, read back.

**Result: the parameter blocks came back byte-identical.** All twelve changed
values persisted. The only difference between the before and after files is the
preset-name string, which is a save-dialog artifact and carries no device state.

So there is no "live but uncommitted" state to lose. The STM32 writes each
parameter into its non-volatile store as it receives it, and re-pushes the whole
map to both ADAU1701s at boot. The ADAU1701s remain volatile — that part was
right — but nothing above them is.

**The corrected model, in the operator's framing, which is the right one:**

| Region | What it is | Survives power loss |
|---|---|---|
| ADAU1701 parameter RAM | what is actually filtering audio | no — re-pushed at boot |
| STM32 working area | the current tune, updated on every write | **yes** |
| STM32 preset slots | named stored tunes, recalled on demand | yes |

A recalled preset **overwrites the working area**. That is what destroys
uncommitted edits — not power loss. The distinction is worth stating precisely
because it inverts the risk: edits are never in danger from a power cycle, and
always in danger from a preset recall.

**Consequences, and the second one is a safety matter:**

1. **M3 needs no commit step.** Write parameters and they stick. `TRANSMITTAL`
   (`DataType 9`, ChannelID 97) is *not* a store-to-flash command, or at least
   is not required for one. Its purpose is still unknown; it remains blocked.
2. **There is no undo-by-power-cycle.** Every write we make is immediately
   permanent, so a tuning run cannot rely on "try it and reboot to revert".
   The improvement invariant's rollback must be an explicit write-back of the
   saved values, verified by readback. This makes the `.DDP` backups load-bearing
   rather than convenient.
3. **Presets are a hardware-level rollback path.** Storing the baseline tune to
   a preset slot before a run gives a one-action restore that does not depend on
   our software being correct. Use it in addition to the file backup, not
   instead of.

##### Settled 2026-08-09: presets are addressed by `user_id`, and **a recall is a READ**

**Source:** `captures/btsnoop_hci_2026-08-09_presets.log`, 28 991 protocol
frames. Every operator action was stamped with the phone's own clock over adb,
and btsnoop timestamps are device time, so each burst maps to its action
directly rather than by inference.

The whole preset mechanism is one header field. `user_id` selects the target:
**0 is the live working area, 1–6 are stored slots.**

| Operation | Frames |
|---|---|
| **Recall slot N** | `0xA1 WRITE dt9/ch5` master volume → 0 · **eight `0xA2 READ dt4/ch0-7/id0 user_id=N`** · `0xA1 WRITE dt9/ch5` volume restored |
| **Store to slot N** | `0xA1 WRITE dt9/ch0/id0 user_id=N` with a 16-byte name · **eight `0xA1 WRITE dt4/ch0-7/id0 user_id=N` of 296 bytes each** |

> ### ⚠ The recall is performed *by the read itself*
>
> There is **no select opcode**. Checked against the unfiltered frame list for
> the whole recall window: the only traffic is the two volume writes, the eight
> read/reply pairs, and the ~10 Hz status poll. Nothing else.
>
> So a frame that is in every visible respect an ordinary read — `0xA2`, zero
> payload, a `DataID` we already permit — **overwrites the entire working tune,
> all eight channels, on a device with no undo.** It is the most destructive
> operation in the protocol short of `RESET_MCU`.
>
> The app knows: it mutes the master volume either side of the eight reads.
> Nobody mutes for a read.
>
> `txpolicy` inspected `user_id` nowhere and listed `DataID` 0 as readable, so
> it permitted this frame. No caller ever set the field, so there was no live
> bug — what there was is the last line of defence holding the door open while
> believing reads are inherently safe. Fixed, with
> `tests/test_txpolicy.py::TestPresetSlotAddressing` pinning it.

**`DataID` 0 is overloaded, and only the length disambiguates it.** An 8-byte
payload is EQ band 0; a 296-byte payload is a whole channel record. The write
path already required exactly `BLOCK_PAYLOAD_LEN`, which refuses the bulk form
— that check is now load-bearing rather than incidental and must not be
relaxed. Note this also means `OUTPUT_BULK_LEN` is real after all, but at
`DataID` 0 with a slot selected, **not** at `DataID` 119. A bulk write to the
*working area* is still unobserved.

**This is the rollback mechanism the improvement invariant has been describing
without having.** Store the baseline to a slot, and one recall restores all
eight channels in ~5 s, verified here by known answer: slot 6 was stored with
the D1 test configuration and recalled two minutes later returning `HPF 20 /
LPF 20000 / band 2 at 27 Hz, +12.0 dB, bw_raw 65` exactly, which no factory
preset could plausibly contain.

##### Corrected 2026-08-09: there are **six** preset slots, not fifteen

The first capture showed the app reading fifteen preset names at `dt9/ch0` with
`user_id` 1–15, and that was recorded as fifteen slots. Earlier this session
that reading was used to suggest slots 7–15 might make ideal rollback storage,
being unreachable from the phone app. **They are not slots at all.**

The second capture read all fifteen again. Slots 1–6 hold distinct names
(`re-timed`, `rockkkkkk`, `- bass`, `lbass`, `test`, and the one just stored).
Slots 7–15 return **one identical name** — and it changed between sessions:
previously `lbass` on all nine, now `d1_lf27` on all nine, which is the filename
of a `.DDP` saved from the *Windows* app hours earlier and never sent to any
slot. Nine identical names tracking something else are a stale buffer being
returned for an out-of-range index. The Android app exposing exactly six slots
agrees.

`PRESET_SLOT_MAX = 6`. Reading a name is harmless, so this cost nothing beyond
a wrong belief — but it would have become a plan to store rollback data in
addresses that do not exist.

Names are 15 bytes on read, ASCII, NUL-padded. The write carried **16** — one
trailing byte, `0x26` in the only sample, meaning unknown. It is not the sum of
the name bytes. Do not assume it is padding.

##### Settled 2026-08-09: a `.DDP` can be loaded back to the device

Until this was observed, `.DDP` files were treated as a *record* — provably
readable, with no demonstrated way back. The operator loaded
`dspcartunebackups_flat_channel_1_diff.DDP` through the vendor app, and **the
device took it.**

Confirmed by measurement rather than by the app's own report, which matters
because the app displaying a loaded file proves only that the app parsed it:

* the state the device held 38 minutes earlier (`eq_channel1_no_mute.DDP`) has
  OUT5 EQ band 1 at **286 Hz, −5.8 dB**, inside the 200–1600 Hz window a
  crossover sweep was then fitted over. A dip that size cannot coexist with the
  0.247 dB LR4 residual measured. It is not there.
* that state also has `l_level` **1** where the loaded file has **3**; an LR4 fit
  that clean is inconsistent with a different rolloff order.
* the load is the only intervening event.

**Consequence for rollback.** There are now two restore paths, and they fail
differently, which is the property that makes having both worth the effort:

| Path | Needs | Survives |
|---|---|---|
| Preset recall | nothing but the device | host loss, app loss, file loss |
| `.DDP` load | host + vendor app + the file | a preset slot being overwritten or unavailable |

Neither is ours. Both are the vendor app's, so both are unavailable to an
automated run — which is exactly why `snapshot.py` exists. What this changes is
the *operator-side* safety net around a bench session: a file backup is now
recoverable, which the improvement invariant was not previously entitled to
assume.

**Closed 2026-08-09, negatively: the `.DDP` load path is USB-only.** The Android
app has no file import at all — only load and save against its six preset slots
— so no Bluetooth capture can ever show it. Answering the wire format of a
whole-device restore now needs a USB analyser, which is a different and much
more expensive experiment. Recorded so nobody plans another HCI capture to hunt
for it.

##### What the same capture settled about the other open questions

| Question | Outcome |
|---|---|
| **Mute on the wire** | `dt4/ch0/id31`, byte 0 `1 → 0 → 1`. An ordinary MISC-block write, no special opcode. Confirms the `.DDP` A/B from the other side. |
| **Master volume** | `dt4`? No — `dt9/ch5/id0`, byte 0. Confirmed twice: once by a deliberate 49→56→49 drag, once by the mute-around-recall. Byte 6 is a flag, 0 while muted. A global control, so a write here moves every channel at once. |
| **Mix matrix writes** | `dt4/chN/id33`, one byte per input. Twelve button presses produced exactly twelve frames — the app writes every intermediate, as it does for gain. |
| **`DataType 3`** | Still zero frames, and now we know why: **the Android app has no input-side controls.** Not a gap in the capture, a gap in the app. Reachable only from the Windows app or from us. |
| **Clean disconnect** | **Still unobserved.** The snoop log is flushed periodically and the bugreport was pulled 33 s after the disconnect; the log ends 25 s before it. Wait two minutes after the last action before pulling. |

##### Settled 2026-08-09: `EqBand.type` is the band shape

**Source:** `dspcartunebackups_c1_{ls_en,hs_en,ls_and_hs_en}.DDP`.

| `type` | Shape |
|---|---|
| 0 | PEQ (peaking) |
| 1 | Low shelf |
| 2 | High shelf |

Each A/B moved exactly one band: enabling the low shelf set `eq[0].type = 1`,
the high shelf `eq[9].type = 2`, and enabling both set both. The app offers the
low shelf only on band 1 and the high shelf only on band 10; **whether the
device would accept a shelf elsewhere is unobserved**, and that is an app
constraint until someone shows otherwise.

Same story as the crossover selectors: the field read 0 on all 112
channel-records because the corpus contained no shelves, not because the field
was dead.

> ### ⚠ This mapping *added* a refusal
>
> A shelf is not a peaking section, and `bw` does not mean what
> `q_from_bw_raw` says it means for one. `optimize.biquad` fits peaking
> biquads, so writing a fitted band into a shelf slot leaves the device running
> a filter nobody modelled.
>
> Nothing would look wrong. The write succeeds, the readback matches, the fit
> was plausible — and the improvement invariant would compare a prediction
> against a differently-configured system and attribute the difference to
> acoustics. `Dsp408Spp` now raises instead, naming the band and what to do.
>
> Carrying the field through blind was the **right** call while it was unknown
> and became a hazard the moment it was known. That is worth noticing as a
> pattern: decoding a field can create an obligation, not just an ability.

##### Settled 2026-08-09: crossover slope and alignment, by fourteen A/Bs

**Source:** `dspcartunebackups_c1_{hpf,lpf}{6,12,18,24}db.DDP` plus
`..._24db_{butterworth,bessel,defeat}.DDP`. One control changed per file.

| `h_level` / `l_level` | Slope | | `h_filter` / `l_filter` | Alignment |
|---|---|---|---|---|
| 0 | 6 dB/octave | | 0 | Linkwitz-Riley |
| 1 | 12 dB/octave | | 1 | Butterworth |
| 2 | 18 dB/octave | | 2 | Bessel |
| 3 | 24 dB/octave | | 3 | **Defeat** — the crossover is bypassed |

`slope = 6 × (level + 1)`. The two are **orthogonal**: every high-pass A/B left
all three low-pass bytes untouched and vice versa, and no corner frequency moved
in any of the fourteen. Both directions are asserted in
`tests/test_bulk_record.py::TestCrossoverSelectorsAreMapped`, because a
single-control A/B is only evidence if the other controls really did hold still.

**Why this looked unmappable for months.** `h_filter`/`l_filter` read 0 on all
112 channel-records because every tune ever saved used Linkwitz-Riley;
`h_level`/`l_level` only ever took 1 or 3 because the operator only ever used 12
and 24 dB/octave. **Absence of variation in the corpus, not absence of meaning.**
No further analysis of the existing backups could have produced this — the
corpus was exhausted. Two minutes of deliberately varying the control did.

##### It corroborates an acoustic measurement, and corrects a docstring

`OutputXover` claimed OUT5's 450 Hz low-pass carries `l_level = 1` and measured
as textbook LR4, offering that as evidence that 1 meant 24 dB/octave. **It
conflated two tunes.** The configuration on the device for that measurement has
`l_filter = 0, l_level = 3` — Linkwitz-Riley, 24 dB/octave, exactly LR4.
`l_level = 1` belongs to preset 4, which was never measured.

So the D0 result (450.1 Hz, 0.247 dB rms against an LR4 fit) is an *independent
acoustic confirmation* of a mapping derived from file diffs. Two methods sharing
no mechanism, agreeing.

It also found a live bug: `dsp408_spp._slope_from_level` returned
`24 if level == 1 else level`, wrong in both branches, and had been reporting
12 dB/octave crossovers as 24.

**Crossover slope is now writable.** `level_raw_for_slope` raises rather than
rounding — a 15 dB/octave request is a caller error, and silently storing 12 or
18 would leave the device disagreeing with the model the optimizer reasoned
about. Alignment is mapped but `ChannelConfig` cannot express it, so it is
carried through unchanged.

##### Settled 2026-08-09: the app mirrors, the device does not

**Source:** `captures/btsnoop_hci_2026-08-09_link.log`. The first attempt at this
missed — the operator's re-link never reached the wire, so the pair spent the
test *unlinked* without anyone knowing. The fix was to test the linked case
**first, while the pair was already linked**, then unlink, since unlinking is
the operation known to be written.

Three gain steps on output 7, linked, then the same three unlinked:

```
linked    22:38:32 ch=6 id=31  01 00 a4 01 00 00 00 0f    gain_raw 420
          22:38:32 ch=7 id=31  01 00 a4 01 00 00 00 12    gain_raw 420
          22:38:33 ch=6/ch=7   ... 9a 01 ...              gain_raw 410
          22:38:34 ch=6/ch=7   ... 90 01 ...              gain_raw 400

unlinked  22:39:35 ch=6 id=31  01 00 86 01 00 00 00 0f    gain_raw 390
          22:39:36 ch=6 id=31  01 00 7c 01 ...            gain_raw 380
          22:39:37 ch=6 id=31  01 00 72 01 ...            gain_raw 370
                   (no ch=7 frames at all)
```

**Six writes linked, three unlinked, for the same three actions.** The device
mirrors nothing; the vendor app sends two writes ~10 ms apart.

Byte 7 differs between the pair — `0x0f` and `0x12`, 15 and 18, each channel's
own `spk_type` — so these are ordinary read-modify-writes of each channel's own
block, not one payload broadcast to two addresses. A backend that broadcast
would silently overwrite `spk_type` on the partner.

**Consequence.** A single write is safe and predictable: it changes exactly the
channel addressed. What the link costs is not a device hazard but a *modelling*
one — write one half of a pair and the device stops matching what the optimizer
reasoned about, and the improvement invariant then measures a prediction against
a differently-configured system. `Dsp408Device.link_partners()` and
`modify_block_mirrored()` exist for that; `txpolicy.refuse_linked_channels`
stays on by default, but its meaning is now "decide whether you meant to mirror"
rather than "nobody knows what this does".

> **⚠ Do not trust the app's link display.** In **two** separate captures the
> app wrote unlinking and never wrote re-linking — `DataID` 35 appears twice per
> session, both times clearing the group. The app can show a pair linked while
> the device has `linkgroup_num` stored as 0. Read the group from the device.

##### Settled 2026-08-09: a clean disconnect sends nothing

The one part of the session lifecycle never observed, because the previous
capture's log flushed 25 s short of it. Waiting two minutes before pulling the
bugreport caught it:

```
22:40:20.108 >dev  poll
22:40:20.144 dev>  reply
                   (nothing further)
```

The app polls at ~10 Hz right to the last millisecond and then stops. **There is
no application-layer goodbye** — not on a link timeout, and not on an explicit
in-app disconnect either. `Dsp408Session.close()` simply closing the transport
matches the vendor app exactly.

#### The three EQ controls, measured

Ladder run 2026-08-08 from a preset-4 baseline, a backup saved after every
action. Files `eq_1_baseline` … `eq_5_restore`.

| Control | What it writes | Reversible |
|---|---|---|
| **Bypass on** | every non-zero band `level` → 600. Frequencies and bandwidths untouched | **yes** — toggling off restored all 15 gains exactly |
| **Bypass off** | writes the prior gains back | — |
| **Reset EQ** | 65 changes: band frequencies back to the default octave layout, all `bw_raw` → 52, all `level` → 600 | **no**, and the app warns first |
| **Restore EQ** | nothing at all after a reset | — |

`eq_1_baseline` and `eq_3_bypass_off` are identical in every output field, so
bypass round-trips perfectly within an app session.

**Restore EQ does not undo a reset.** There is no shadow copy; a reset is final.
What Restore EQ *is* for remains unknown — it may be paired with bypass, or with
a per-session snapshot we have not triggered.

Bypass is per-channel: outputs 7 and 8 carried no EQ, so bypassing them was a
no-op, and in an earlier run output 4 was skipped and stayed untouched.

##### The hazard is the backup, not the button

Bypass is not destructive in the app — but **the stored band levels really are
zeroed while it is engaged, so a `.DDP` saved during bypass does not contain the
EQ gains.** The app restores them from somewhere the file does not carry.

That makes a backup taken while bypassed silently incomplete, and it is not
reliably detectable after the fact: a band sitting at a custom frequency with
exactly 0 dB is also a perfectly ordinary thing for a tuner to leave behind —
the operator's own tune contains several. **Never save a backup with bypass
engaged.**

##### Closed: the undo lives in app memory only

**Operator test, 2026-08-08.** Load preset 1 → bypass PEQ on channel 3 →
unplug USB and reconnect. **The restore option is gone**; channel 3's gains
cannot be recovered. Repeated with a device power cycle instead of a
reconnect: same outcome, and the rest of preset 1 comes back normally while
channel 3 keeps the zeroed values.

The mechanism follows, and it is the operator's reading:

- The app builds an **in-memory model of the tune when it connects**, by reading
  the device. Bypass keeps the pre-bypass gains only in that model.
- Edits go straight to the device's working area, which is non-volatile.
- **The device holds no shadow copy.** Restore EQ is not a device feature; it is
  the app replaying values from its own session memory.
- Anything that ends the session — unplugging USB, closing the app, cycling
  device power — discards those values permanently.

So **bypass is a session-scoped undo, not a device feature**, and it is
destructive the moment the session ends. The earlier framing of "reversible" was
true only within one continuous connection.

Two consequences worth carrying:

1. **A client we write has no undo at all.** Our own backend will not have the
   app's session memory, so every write it makes is immediately permanent and
   irrecoverable from the device side. The `.DDP` backups are the *only* undo
   this project will ever have, which makes them a correctness requirement of
   M4's rollback rather than a convenience.
2. **Presets are unaffected by edits and are the one device-side restore.**
   Preset 1 came back clean after being bypassed *and* power-cycled, twice.
   Editing the working area does not write through to the preset slot it was
   recalled from, so a preset is a stable rollback point that survives
   everything. Store the baseline into a slot before any tuning run.

##### A note on how this finding was corrected twice

First written up as "Bypass EQ is destructive", on an operator statement about
which button was pressed. The operator then flagged that Reset was the likelier
culprit, and the attribution was withdrawn. The ladder shows **both corrections
were wrong in different directions**: the earlier action *was* bypass — its
signature is level-only, while reset also rewrites frequency and bandwidth — but
bypass is reversible, so "destructive" was the wrong conclusion to draw from it.

The byte-level signature settled what neither recollection could. Worth
remembering that a diff distinguishes actions by *what they wrote*, which is
evidence of a different kind from what anyone remembers clicking.

#### Vendor backup file format (`.DDP`)

Decoded 2026-08-08 from the operator's backup. Implemented in `tuner.dsp.ddp`,
dumped and diffed by `tools/ddp_dump.py`.

```
offset 0        u8      length of the magic string (16)
offset 1..16    ascii   "DSA-4.8_File_3.0"
offset 17..48   ascii   preset name, NUL-padded to 32 bytes
offset 49       553 8-byte parameter blocks:
  blocks   0..32    global / unidentified          (33 blocks)
  blocks  33..248   6 input records of 36 blocks
  blocks 249..544   8 output records of 37 blocks
  blocks 545..552   8 trailing name blocks
```

An output record is 37 blocks = 296 bytes, which is exactly `OUTPUT_BULK_LEN` —
the DataLen the app uses to write a whole channel in one frame. The record is
the wire layout, in file form: 31 EQ bands, then misc, xover, mix, two dynamics
blocks, name.

**This makes the file a zero-risk readback channel.** Change exactly one control
in the vendor app, save a second backup, diff the two, and that control's raw
encoding falls out with nothing written by us and no stimulus played. It is the
same move that settled the delay question, generalized into a tool.

Input records are carried through undecoded: the 36-block stride is solid, but
every input in the only available sample is at its default, so nothing pins the
field layout.

#### Still open

* Units and ranges for `gain`, `delay`, `level`, `bw` — the encodings are
  known, the scaling is not. The backup narrows these to a small number of
  hypotheses; see `docs/STATE.md`.
* The full `DataType` map. 3 (input), 4 (output) and 9 (system) are decoded;
  others may exist.
* ~~**Which characteristic to write.**~~ **Moot** — the app uses RFCOMM, not
  GATT. Kept below only because the explanation is useful.

  > ### Hypothesis 2026-08-09: the GATT service belongs to the dongle, not the DSP
  >
  > The DSP-408 has no Bluetooth radio. The **DSP-BT5.0 dongle** does, and the
  > MCU reaches it over SPI through the USB-A-shaped port — a connector that
  > carries SPI, not USB, which is why generic dongles get no power there.
  >
  > `0xFFE0`/`0xFFE1` is the stock transparent-serial profile that HM-10-style
  > and BK-series Bluetooth modules ship with. Our unit advertising `0xFFE0`
  > while exposing `0xFFF0` with `0xFFE1` notify and `0xFFE2` write is a
  > vendor variant of exactly that pattern.
  >
  > **So the GATT service is very likely the module's own firmware, generic
  > and unrelated to the DSP-408's protocol.** That accounts for all three
  > observations at once: a writable characteristic exists, the vendor app
  > never touches it, and the SDP scan was inconclusive. The app talks RFCOMM
  > because that is the path the *product* was built around; the GATT profile
  > is what the radio happens to offer.
  >
  > It also predicts that **the BLE path would work**: a transparent-serial
  > bridge lands on the same SPI link, the same MCU, and the same frames we
  > already encode byte-for-byte. Untested, off the critical path, and not
  > worth the only unit — but if a spare dongle or unit ever appears, this is
  > the cheap experiment.
  >
  > **Inference, not measurement.** Confirming it needs no more than a photo
  > of the module's markings inside the dongle.
* Channel-to-chip mapping (still needs the firmware, or measurement).


### 2026-08-06 (late) — Firmware is published, unencrypted, and contains the DSP program

Dayton publish the firmware image on the product page. It needs no hardware,
no SWD, and no readout-protection bypass.

* `DSP-408-Firmware-Update-6.21.bin` — 70 296 bytes
* `DSP-408-Windows-V1.24 190622.zip` — contains a single 54 MB `DSP-408.exe`

**Structure of the firmware image:**

| Offset | Content |
|---|---|
| `0x0000` | 8-byte header, magic **`WMCU`** (`57 4d 43 55 08 00 50 00`) |
| `0x0008` | ARM Cortex-M vector table — initial SP `0x200049A0`, reset `0x08005101` |
| body | STM32 Thumb code; 54/63 vector entries are valid in-image thumb addresses |
| → `0x0800F84D` | highest referenced code address |
| body `0x0D800` | **ADAU1701 program image** (see below) |
| body `0x0E000` | 2 KB of zeros |

Load base is `0x08000000` with the 8-byte header stripped.

**It is not encrypted.** Overall entropy 6.32 bits/byte, falling to 2.22 in the
tail; encrypted data would sit near 7.99 uniformly.

**A 5600-byte region at body `0x0C540`–`0x0DB20` has strong 5-byte
periodicity** — 978 of 1120 records begin `0x01`, versus 0.2–1.2% of bytes at
the other four phases, while ARM code elsewhere is flat across all phases. The
structure is real and statistically unambiguous.

> **What it is remains unidentified.** An earlier revision of this document
> claimed it was the ADAU1701 program image, on the strength of 5-byte records
> matching the chip's 1024 × 5-byte program RAM. **That claim was not
> supported and has been withdrawn.** Testing the 4-byte payloads as 5.23
> fixed point puts only 59% in a plausible range, and the values are ascending
> 16-bit counters stepping by 5 — characteristic of an address or offset table,
> not filter coefficients. Structure alone does not identify content.

**No I²C peripheral is used.** Neither `0x40005400` (I2C1) nor `0x40005800`
(I2C2) appears in the literal pool, while GPIOA/B/C appear 27 times between
them. The ADAU1701s are therefore almost certainly driven by **bit-banged I²C
on GPIO**, which means the I²C addresses will not be found as peripheral
constants and must be recovered from the bit-bang routine itself.

**USART1, USART2 and USART3 are all in use** — plausibly the BT dongle, the
DSP-RC remote, and one other. Worth mapping.

**No literal-pool pointer into the 5-byte region was found**, which is itself
evidence against the program-image reading; a download routine would normally
reference its source address directly.

#### What this replaces

The Tier 2 hardware plan — $3 ST-Link, SWD probing, soldering, and a possible
fight with STM32 readout protection — is **unnecessary for firmware access**.
The "jackpot" identified from the teardown is a public download.

Extractable offline, with no hardware and no risk:

* The BLE frame handler, and therefore the protocol on `0xFFE2`/`0xFFE1`.
* The bit-banged I²C routine, and with it the ADAU1701 addresses — which
  settles whether there are one or two chips and how channels map to them.
* Potentially the DSP program and parameter map, once the 5-byte region is
  identified.

**But this needs proper tooling.** A resilient Thumb sweep recovers 20 380
instructions, which is enough for constant hunting and not enough for
function-level understanding. Real progress here means Ghidra and a serious
time investment in ARM reverse engineering.

**The APK is the better investment.** Java decompiles to readable source;
ARM decompiles to a week of work. Both contain the same protocol, and we now
know precisely what to search for: writes to characteristic `0xFFE2`. The
firmware should be treated as corroboration for whatever the APK reveals,
not as the primary route.

#### Still not attempted

No bytes have been written to the device and nothing has been flashed. The
firmware file is for **static analysis only** — flashing it serves no purpose
here and is the one action that could brick the unit.


### 2026-08-06 (evening) — BLE transport identified · unit powered, no audio cables

> **⚠ Superseded 2026-08-08.** The conclusion below — that control is BLE-only —
> is **wrong**. An HCI capture of the vendor app shows classic Bluetooth RFCOMM
> and essentially no BLE traffic. The scan results recorded here are accurate as
> far as they go; the inference drawn from them was not. A scan says what a
> device *offers*, a capture says what the software *uses*. Kept as a record of
> how the wrong answer looked convincing.

**The control transport is BLE GATT.** Confirmed by scanning and connecting to a
powered DSP-408 with a DSP-BT5.0 dongle fitted.

| Property | Value |
|---|---|
| BD address | `00:13:EF:A0:09:10` (dual-mode) |
| BLE advertised service | `0xFFE0` |
| Control service | `0xFFF0` (vendor specific) |
| **Host → device** | char **`0xFFE2`**, `write-without-response`, handle 17 |
| **Device → host** | char **`0xFFE1`**, `notify`, handle 14 |
| Negotiated MTU | 120 (~117 byte payload) |
| Firmware revision (`0x2A26`) | **1.1.9** |
| Device name (`0x2A00`) | `DSP-408` |

`0xFFE0`/`0xFFE1` is the well-known HM-10 / TI CC254x transparent BLE-UART
pattern, here split so that writes go to `0xFFE2` and responses arrive as
notifications on `0xFFE1`.

~~**Classic Bluetooth carries no control channel.** The same address pairs over
classic BR/EDR but advertises only A2DP sink (`0x110B`) and AVRCP
(`0x110C`/`0x110E`) — audio streaming only. There is no SPP (`0x1101`) and
Windows creates no COM port for it. Control is BLE-only.~~

> **This paragraph is the wrong one.** A capture of the vendor app shows it
> using exactly the SPP that this scan said did not exist — RFCOMM on server
> channel 1, ~11 800 frames of it. Either the SDP query was incomplete, the
> device does not advertise the record it nonetheless serves, or the scan was
> run against a state where it was absent. The failure mode worth remembering:
> **a negative result from a scan is much weaker evidence than a positive one
> from a capture**, and this one was written down as though it settled the
> question.

**The device is request/response; it volunteers nothing.** Twenty seconds
subscribed to `0xFFE1` on a fresh connection produced zero notifications, so the
framing cannot be learned by passive listening. Something must be written to
`0xFFE2` first.

#### Why this matters

Transport is now a solved problem, and it solved better than expected:

* **No drivers, no vendor DLL, no USB stack.** Any host with a BLE radio and
  `bleak` can talk to the unit — Windows, Linux, or a Pi, with identical code.
* The earlier concern that the USB-A port carries SPI rather than USB is now
  moot for control purposes. The USB-B port and its STM32 USB device remain a
  second, independent route worth characterizing, but nothing depends on it.
* **The APK decompile is now a targeted search rather than a fishing trip.** We
  know exactly what to look for: the code that writes to characteristic
  `0xFFE2` and builds those frames.

#### Not attempted, deliberately

No writes were made. The only DSP-408 available is the operator's in-service
unit, and writing undecoded bytes risks corrupting a working tune. Sending
frames should wait for either (a) a decoded protocol from the APK, so the bytes
have known meaning, or (b) the spare unit, or (c) a vendor-app backup of the
current configuration that can be restored.


### 2026-08-06 — Teardown photograph analysis · no hardware required

Source: [cyberpithilo teardown](https://cyberpithilo.web.fc2.com/audio/openit/Dayton_dsp/index.html) (Japanese), board photographs read directly. Cross-checked against the official user manual.

**Confirmed:**

| Finding | Evidence |
|---|---|
| **2× ADAU1701JSTZ** on a DSP daughtercard | Both legible in `dspcardtopview.jpg` |
| **STM32F103C8T6** as bridge MCU (LQFP-48, 64 KB flash, native USB FS) | Marking legible after rotation and contrast enhancement |
| **12.288 MHz** oscillator = 256 × 48 kHz | Silkscreen on DSP card |
| **USB-A port carries SPI, not USB** | Teardown text; corroborated by Dayton's dongle exclusivity |
| **USB-B is a separate, real-USB PC port** | Manual connector list; USB-B receptacle visible on mainboard |
| 4× NE5532 output buffers; 4 relays for high-level input switching | DSP card and mainboard photos |
| No self-boot EEPROM on the DSP card | Absent from both card faces |
| Unpopulated 2×5 header + two 4-pad groups on DSP card underside | `dspcardbottomview.jpg` |
| Board revisions: mainboard `DSP-408 180808 V1.1`, DSP card `YDW-DBS480-DSP-CT2` | Silkscreen |

> ### ⚠ That teardown is of a **different revision**, and two rows above are wrong for our unit
>
> Photographed 2026-08-10, our own hardware, close enough to read every marking
> (`Board Images/2026-08-10 closeups/`). Revisions: mainboard
> **`DSP-408 200504 V1.3`**, DSP card **`YDW-DBS480-DSP-C121-XLB-069 180301`**.
> The teardown's unit was `DSP-408 180808 V1.1` / `YDW-DBS480-DSP-CT2`.
>
> | Teardown says | Our unit | Evidence |
> |---|---|---|
> | STM32F103C8T6 (ST) | **Geehy APM32F103C8T6** — a pin-compatible clone | `2026-08-10_dspcard_mcu-apm32f103-and-8mhz-xtal_01.jpg` |
> | "No self-boot EEPROM on the DSP card" | **Atmel `ATMLH146 2ECL CN 2148DUG`** SOIC-8, an AT24C-series I²C EEPROM, sitting beside the ADAU pair | `2026-08-10_dspcard_adau-eeprom-and-4k7-pullups.jpg` |
>
> Both hold up: 2× ADAU1701JSTZ (`#2143`), 4× NE5532 output buffers arranged
> two per DSP, an unpopulated 2×5 header between the DSPs, and each ADAU with
> its own oscillator and `CJT1117B` 1.8 V LDO. Also visible and new: a 4-pad
> through-hole row beside the MCU, and **two 4.7 kΩ (`472`) resistors** next to
> the EEPROM, which is the pull-up signature of an I²C bus.
>
> **What this changes.**
>
> 1. **An SWD dump is not a drop-in.** Geehy silicon has its own device ID, and
>    an ST-Link may need coaxing or refuse outright. Budget for that before
>    buying on the strength of "it's an F103".
> 2. **The "STM32 downloads both programs at boot" inference is void**, because
>    it followed from the absent EEPROM. With an EEPROM present the ADAUs may
>    self-boot and the MCU may only write parameters. Either way there is a
>    control bus to watch, so the plan survives — but the reason for it does not,
>    and an inference whose premise has gone has to be re-derived rather than
>    carried.
> 3. **Do not read "8 MHz" as the audio clock.** The `HQ8.000M` crystal is the
>    MCU's; each ADAU has its own oscillator, and the ADAU1701 is fixed at
>    48 kHz regardless.
>
> The general point, and it is the second time this project has paid for it:
> **a teardown of "the same product" is evidence about the unit that was torn
> down.** Board revisions three years apart are different hardware.

### Logic-analyser session, 2026-08-10 — what the EEPROM bus does, and does not, carry

Saleae Logic (VID_0925/PID_3881) on the AT24C EEPROM's own SDA/SCL, ground on
its pin 4. Pin identification was purely by meter: exactly one pin reads a dead
short to chassis (0.5 ohm), which fixes pin 4, and the SOIC-8 numbering does
the rest. Rig validated first by a touch test — the channel read steady high on
VCC and steady low on ground, which is what distinguishes a real reading from a
floating input.

**Measured, three captures:**

| | |
|---|---|
| Bus | I2C, ~333 kHz, **one device: `0x50`** |
| Addressing | 16-bit — 40 read bursts, 2 address bytes each |
| At power-on | ~4.3 KB read out of the EEPROM in a single 0.23 s burst, about 5 s after power is applied. A program load |
| On a gain step | **one 10-byte write**: address `0x0398`, then 8 data bytes |
| Anything else | **Nothing. No ADAU is ever addressed on this bus** |

**A one-step gain change writes exactly one byte.** Up then down:

    03 98 | 34 00 00 32 00 32 01 00     one step up
    03 98 | 33 00 00 32 00 32 01 00     one step back down

Same address, one byte moving by one count and returning. So the EEPROM stores
gain as a **single byte, one count per app step**, where the wire protocol uses
a `u16` at 0.1 dB per count. The absolute offset is **not yet established** —
`dB = byte - 60` would make these -8 and -9 dB, which is plausible but
unconfirmed, and the DDP corpus has output 1 at -10 or -12 dB depending on the
file, so guessing from those would pick the wrong one. One question to the
operator closes it.

The payload is **not** an `OutputMisc` block: decoding it as one yields
`gain_raw=12800` and +1220 dB. The EEPROM has its own layout, and only the one
byte has been identified.

#### This is "every write is immediately non-volatile", caught in the act

The project recorded that rule on 2026-08-08 from a power-cycle test — change a
value, pull the plug, the value survives. Now the mechanism is visible: **the
MCU commits an EEPROM write on every individual slider step.** Two clicks 1.5 s
apart produced two writes.

That converts the "device is never inside the optimizer's inner loop" rule from
an argument into a measurement. A fitter that wrote per iteration would spend
EEPROM cycles, not merely time, and the endurance figure for the part is still
unknown and must not be invented.

#### FOUND: the ADAU control bus is the 2x5 header, pins 9 and 10

**SCL = header pin 10, SDA = header pin 9.** I2C, and **two devices: `0x35`
and `0x37`.** Traffic is continuous — the MCU polls both chips for the whole
life of the capture, not only when something changes.

> **The earlier "the header is dead" result was a wiring artifact, not a
> finding.** Seven channels read flat because they had been soldered to the
> underside of pads that are top-side only, so they were connected to nothing.
> Re-done on the top side, two of the same pins are alive. The methodological
> error is worth naming: the rig's touch test validated **one** channel's path
> (probe on VCC reads high, probe on ground reads low) and the other seven were
> then trusted on no evidence. A validated channel plus seven unvalidated ones
> is one validated channel, and silence from an unverified probe is not a
> measurement. Continuity from each clip to its pad, before believing a null.

#### Output 1 is on the ADAU at `0x37`, measured

Ten one-step gain edits (50 up to 55 in the app, then back) produced ten
writes, **all to `0x37`, none to `0x35`**:

    08 10 | XX XX XX | 08 15 | 01 4D | 08 1C

`0x0810` is the parameter address; the three payload bytes are the coefficient.
Interpreted as **5.23 fixed point** (`value / 2**23`, linear gain) they are
exact:

| app | raw | linear | dB |
|---|---|---|---|
| 50 | `28 7A 26` | 0.316228 | **-10.0000** |
| 51 | `2D 6A 86` | 0.354813 | -9.0000 |
| 52 | `32 F5 2D` | 0.398107 | -8.0000 |
| 53 | `39 2C ED` | 0.446684 | -7.0000 |
| 54 | `40 26 E7` | 0.501187 | -6.0000 |
| 55 | `47 FA CD` | 0.562341 | **-5.0000** |

Exact to four decimal places over five steps in each direction, so:

- **The app's displayed number is `dB + 60`**, one step per dB. Same -60 offset
  as the wire protocol's `raw/10 - 60`, and it agrees with the DDP corpus,
  which has output 1 at -10.0 dB in `dspcartunebackups_Channel4_preset.DDP`.
- **The EEPROM byte is the displayed number**, stored directly: `0x32` = 50.
  (The earlier capture caught `0x33`/`0x34` because the gain was resting one
  step higher at the time, after a step was lost during a USB drop.)
- **ADAU coefficients are 5.23 fixed point linear gain**, confirmed by
  measurement rather than from the datasheet.

#### THE CHANNEL-TO-CHIP MAP, MEASURED 2026-08-11

    ADAU at I2C 0x37  ->  outputs 1, 2, 3, 4
    ADAU at I2C 0x35  ->  outputs 5, 6, 7, 8

Every output stepped one gain click while the ADAU control bus was captured,
and the chip that received the write recorded. Outputs 1, 2 and 7/8 are pinned
by **unique** coefficient values (—10/—9, —16/—15 and —23/—22 dB), so they need no
inference at all. Outputs 7 and 8 wrote **twice** on each step — the linked
subwoofer pair, both members, both on `0x35`.

**Outputs 3-6 were then falsified rather than assumed.** All four had been
stepped with the same values (`48-49-48`), so their attribution rested on the
order they were edited in — and the answer landed exactly on the split the
project had spent weeks refusing to guess, which is when to distrust yourself
hardest. Re-running them in **reverse order** (6, 5, 4, 3) produced the same
channel-to-chip assignment in the opposite sequence position:

| edit order | ch6 | ch5 | ch4 | ch3 |
|---|---|---|---|---|
| forward run | `0x35` | `0x35` | `0x37` | `0x37` |
| reverse run | `0x35` | `0x35` | `0x37` | `0x37` |

The assignment follows the channel, not the position. The obvious guess was
right — and it is now a measurement, which is a different thing.

**Consequences:** `DeviceLimits` and `SimulatedDsp` model one global delay pool
and are wrong in both directions. They can now be corrected to **two pools of
four**, outputs 1-4 and 5-8. Program space is likewise per chip. The exact
figures for either pool remain unmeasured; only the *grouping* is settled.

#### Where the ADAUs are **not**

- **Not on the EEPROM bus.** Three captures, zero frames to any address but
  `0x50`, including through a live gain change.
- **Not on the 2x5 header, pins 1-7.** All seven probed simultaneously through
  a gain step: **zero edges**, six idling high and one low. An unpopulated
  debug header, idle until something attaches — consistent with SWD. Pins 8-10
  were not probed.

So a gain change reaches the ADAUs over a link that is neither of those, and it
is not a re-boot either: a re-boot would re-read the EEPROM, and the capture
shows a write with no read following it.

**Still open, and the next thing to find.** Candidates in cost order: header
pins 8-10; a second EEPROM and a second bus (there is another SOIC-8 near the
ADAU pair that has not been identified); direct MCU-to-ADAU traces requiring
0.5 mm probing.

**Inferred, not confirmed:**

- ~~The STM32 downloads both ADAU1701 programs over I²C at boot (follows from the absent EEPROM).~~ **Void** — the premise is false on our revision, see above.
- The 2×5 footprint is SWD and/or an ADI USBi header.
- Output-to-chip mapping is 1–4 / 5–8. **Still a guess, and still not to be built on.**

**Consequences for the plan:**

1. **Tier 2's priority target changed** from a CH341A EEPROM read to a $3 ST-Link SWD dump of the STM32. Higher payoff, lower cost.
2. **USBPcap remains viable but only on the USB-B port.** Pointing it at the USB-A port would capture nothing, because nothing there is USB.
3. **The `YDW`/`DBS480` silkscreen matches the Android package `leon.android.chs_ydw_dcs480_dsp_408`**, confirming the shared OEM platform and strengthening the sibling-rebrand fallback for APK analysis.
4. Roughly half of Phase D's questions were answered from the manual at zero cost; see "Known from the manual" above.

**Not yet attempted:** APK decompilation, Windows app triage, any live capture.

## References

- ADAU1701 datasheet — Analog Devices. Cite the revision used; do not quote figures from memory.
- [ADI EngineerZone: 4x8 DSP using two ADAU1701](https://ez.analog.com/dsp/sigmadsp/f/q-a/114842/4x8-dsp-using-two-adau1701)
- [MCUdude/SigmaDSP](https://github.com/MCUdude/SigmaDSP) — Arduino I²C library for the ADAU1401/1701/1702
- [freeDSP](https://freedsp.github.io/) — open ADAU1701 hardware and I²C tutorials
- [Dayton Audio DSP-408 product page](https://www.daytonaudio.com/product/1551/dsp-408-4x8-dsp-digital-signal-processor-for-home-and-car-audio)
- [Dayton Audio DSP Control on Google Play](https://play.google.com/store/apps/details?id=leon.android.chs_ydw_dcs480_dsp_408)
- [DSP-BT5.0 Bluetooth dongle](https://daytonaudio.com/product/2200/dsp-bt5-0-bluetooth-data-and-streaming-usb-interface-for-dsp-408)
