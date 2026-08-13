# Board photographs

    <yyyy-mm-dd>_<assembly>_<subject>[_<nn>].jpg

`assembly` is `full` (whole unit), `mainboard`, `dspcard` (the YDW daughter
card), or `interfaceboard`. `subject` is lowercase kebab and names what the
photograph is **evidence about**, not where the camera was pointed.

Dated first because a board revision is a fact about a moment: two units, or
the same unit after rework, have to be tellable apart. Assembly second because
that is how a search narrows. Subject last because it is the part anyone reads.

Renamed wholesale on 2026-08-10; `docs/board-probing.md` cites the new names.

## What is in this unit

| | |
|---|---|
| Mainboard | `DSP-408 200504 V1.3` |
| DSP card | `YDW-DBS480-DSP-C121-XLB-069 180301` |
| MCU | **Geehy APM32F103C8T6**, LQFP-48, 8 MHz crystal (`HQ8.000M`) |
| DSP | **2× ADAU1701JSTZ**, `#2143`, each with its own oscillator and `CJT1117B` 1.8 V LDO |
| Non-volatile | **Atmel `ATMLH146 2ECL CN`** SOIC-8 (AT24C-series I²C EEPROM) beside the DSP pair |
| Output buffers | 4× TI `NE5532` on the mainboard, two per DSP |
| Headers | unpopulated 2×5 between the DSPs; a 4-pad row near the MCU |

**Two of these contradict the third-party teardown** recorded in
`docs/dsp408-protocol.md`, which was of an older revision
(`DSP-408 180808 V1.1`, card `YDW-DBS480-DSP-CT2`): that unit had **no EEPROM**
and a genuine **ST** STM32F103C8T6. Ours has an EEPROM and a Geehy clone. See
the "Board revisions differ" note in that document.
