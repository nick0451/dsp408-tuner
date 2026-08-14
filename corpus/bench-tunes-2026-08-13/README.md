# Two bench tunes, both judged good, 2026-08-13

`.DDP` exports of the first two tunes this project produced end to end and a
person then approved by listening. **Not the car** — a Logitech THX 2.1 on a
desk, fed from DSP outputs 1 and 2.

| file | target | filters | verdict |
|---|---|---|---|
| `harmin_curve_first_pass.DDP` | Full range + room curve (LF rise 1 dB/oct below 200 Hz, HF fall 0.5 dB/oct above 1 kHz) | 7 cuts, largest −5.2 dB | *"the tune is very nice"* |
| `smile_preset.DDP` | the same, with the HF fall inverted to a **rise** — +5.8 dB at 20 Hz, +3.4 dB at 16 kHz relative to 1 kHz | 9 (L) and 8 (R) cuts, largest −12.5 dB | *"second nailed tune"* |

## Why they are worth keeping

**They are the only end-to-end evidence that the whole chain works** — REW
measures, the fit runs under the DSP-408's real constraints, our backend
writes, a re-measurement scores it, and a listener agrees. Every other
validation in this project stops short of the last step.

**And they record a system whose dominant feature nobody expected.** Both
channels show a **15–18 dB peak at 50 Hz**, narrow, consistent across five
microphone positions. Consistency across position is what rules out a room
mode; that is the subwoofer's **port tuning**. It is why the smile tune's
largest filter is a −12.5 dB cut, and why a "smile" on this system sounds
like less boom rather than more bass.

The two channels are also **not symmetric** — R measured +7.9 dB at 500 Hz
where L measured +3.0 — which is why the smile tune has a separate fit per
channel and the Harman one does not.

## What they are not

**Not validated in the car**, and the settings must not be carried there. The
bench pair is fed full range with the crossovers opened to 20 Hz – 20 kHz,
because the plate amp does its own splitting. The car's drivers have **no
passive crossovers at all** — see [docs/car-system.md](../../docs/car-system.md).

**Not level-matched to each other.** Every filter is a cut, so both tunes are
quieter than flat: the Harman one by a measured 2.23 dB where music has its
energy. `tools/bench_ab.py` compensates with channel gain, which is what made
the listening comparison about tonality rather than loudness.
