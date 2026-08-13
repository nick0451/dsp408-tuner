# Firmware analysis pipeline

Reproduces the Ghidra decompilation of the DSP-408 firmware. Nothing here is
part of the tuner; it exists to answer the Milestone 0 questions in
`docs/dsp408-protocol.md`.

## Input

`DSP-408-Firmware-Update-6.21.bin`, published by Dayton on the product page.
Strip the 8-byte `WMCU` header before importing; the remainder loads at
`0x08000000`.

```bash
python -c "open('fw-stripped.bin','wb').write(open('fw-6.21.bin','rb').read()[8:])"
```

## Run

```bash
export JAVA_HOME=/c/Program\ Files/Java/jdk-21.0.11
"$GHIDRA/support/analyzeHeadless.bat" ./proj dsp408 \
  -import fw-stripped.bin \
  -processor "ARM:LE:32:Cortex" \
  -loader BinaryLoader -loader-baseAddr 0x08000000 \
  -scriptPath ./tools/ghidra \
  -preScript SeedAll.java \
  -postScript ExportDecomp.java ./decomp.c \
  -deleteProject
```

## Why SeedAll is necessary

The binary loader supplies no entry points, so auto-analysis alone finds only
82 functions out of a ~70 KB image. `SeedAll` seeds from two sources:

1. **The Cortex-M vector table** — every word whose bit 0 is set is a Thumb
   entry point. Worth 22 functions.
2. **Thumb function prologues** — `PUSH {..., LR}` (`0xB5xx`) and its
   32-bit form `PUSH.W` (`0xE92D 0x4xxx`). Worth another 63.

It also sets the `TMode` register at each seed, without which Ghidra
disassembles Thumb code as ARM and produces nonsense. Result: **278 functions**.

## Findings so far

* StdPeriph-style HAL. `FUN_08001b96` = `GPIO_SetBits`,
  `FUN_08001b92` = `GPIO_ResetBits`.
* No I2C peripheral is used; the ADAU1701s are driven by **bit-banged I2C on
  GPIO**. Peripheral bases appear only in the literal pool, so search for
  functions referencing the *literal slot address* (e.g. `DAT_08001474`), not
  the value `0x40010800` — the decompiler renders the load site, not the value.
* USART1, USART2 and USART3 are all in use.
* `FUN_08007c84` toggles **PA8** as a push-pull output; a bit-bang candidate.

Open: the I2C addresses, and therefore the chip count and channel-to-chip
mapping.
