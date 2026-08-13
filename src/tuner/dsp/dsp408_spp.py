"""DSP-408 control over classic Bluetooth RFCOMM (SPP).

The top of the stack: engineering units in, verified block writes out. Below it
sit :mod:`tuner.dsp.device` (records and read-modify-write),
:mod:`tuner.dsp.session` (transactions), :mod:`tuner.dsp.framing` and
:mod:`tuner.dsp.transport` (bytes). ADAU1701 and wire-protocol details must not
leak above this line.

Transport is **classic Bluetooth RFCOMM on SPP server channel 1** -- a serial
socket, not a GATT client. ``bleak`` is not a dependency. How the channel
number was established, and why the BLE constants below are kept, is at the
bottom of this file.

What this backend will and will not set
---------------------------------------
``ChannelConfig`` is a **view**, not a complete state. It cannot express
polarity, ``spk_type``, mix, dynamics, channel name, link group, or EQ bands
11-30, and the device has fields whose meaning is still unknown. So the rule
is: write what is measured, carry through everything else, and **raise rather
than silently ignore** a request this backend cannot honour.

=====================  ==========================================
Set from ChannelConfig  Carried through untouched
=====================  ==========================================
gain (measured)         polarity, ``spk_type``, ``eq_mode``
delay (measured)        crossover filter and slope bytes
crossover frequencies   mix, dynamics, name, link group
PEQ freq/gain/Q
mute (measured)
=====================  ==========================================

Three refusals worth knowing about before you meet them:

* **Crossover slope is writable as of 2026-08-09**, but only at the four
  slopes the device stores -- 6, 12, 18 and 24 dB/octave. Anything else raises
  rather than rounding to a neighbour, because a silently-substituted slope
  makes the device disagree with the model the optimizer reasoned about.
  Alignment (Linkwitz-Riley / Butterworth / Bessel / Defeat) is carried through
  unchanged; ``ChannelConfig`` cannot express it.
* **A band stored as a shelf will not be written.** ``EqBand.type`` was mapped
  the same day, and a shelf is not a peaking section -- ``bw`` does not even
  mean the same thing. Writing a fitted band into one leaves the device running
  a filter nobody modelled, with a successful write and a matching readback to
  hide it. Set the band to PEQ in the vendor app, or exclude it from the fit.
* **Band placement has no default.** See :class:`PeqPolicy`.

Note the asymmetry between the first two: mapping the slope byte *removed* a
refusal, mapping the type byte *added* one. Decoding a field can create an
obligation as easily as an ability.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum

from .backend import (
    Biquad,
    ChannelConfig,
    Crossover,
    DeviceLimits,
    DspBackend,
    FilterType,
)
from .device import BLOCK_LEN, Dsp408Device
from .protocol import (
    EqBand,
    EqBandType,
    OutputBlock,
    OutputMisc,
    OutputMix,
    OutputXover,
    UnsendableFrame,
    bw_raw_for_q,
    gain_dbfs,
    gain_raw_for,
    level_raw_for_slope,
    q_from_bw_raw,
    slope_db_per_octave,
)
from .session import N_OUTPUTS, DeviceIdentity

#: EQ bands the device addresses per output. Addressable is not affordable --
#: see ``DeviceLimits.max_peq_per_channel``.
ADDRESSABLE_BANDS = 31

#: ``level`` encoding origin: 600 is 0 dB, shared with channel gain.
FLAT_LEVEL_RAW = 600


class BackendError(RuntimeError):
    """Base for engineering-units backend failures."""


class UnsupportedRequest(BackendError):
    """The caller asked for something this backend will not do.

    Raised rather than ignored. A backend that quietly drops a field the
    caller set produces a device that does not match the model the optimizer
    is reasoning about, and the improvement invariant then compares a
    prediction against a system that was never configured as predicted.
    """


class PeqPolicy(Enum):
    """What to do about EQ bands the caller did not mention.

    There is no good default, so there is no default at all -- constructing a
    backend without choosing fails.

    ``ChannelConfig.peq`` is an ordered tuple with no band indices, and the
    device has 31 addressable slots that may already hold a tune. The two
    reasonable readings pull opposite ways:

    ``LEADING``
        Write bands 0..n-1 and **leave the rest alone**. Safer for the device
        -- fewer writes, nothing unexpected disturbed -- but the acoustic
        result is not what the optimizer modelled, because leftover bands are
        still in circuit. That quietly undermines the improvement invariant's
        premise that the measured system matches the predicted one.

    ``EXCLUSIVE``
        Write bands 0..n-1 and **flatten the remainder** to 0 dB, so the
        channel holds exactly the requested chain. Matches the model, at the
        cost of many more writes and of erasing a tune the operator may have
        wanted.

    Pick deliberately, per run, and record it in provenance.
    """

    LEADING = "leading"
    EXCLUSIVE = "exclusive"


def _band_to_eq(band: Biquad, stored: EqBand) -> EqBand:
    """One engineering-units band as a device EQ block.

    Bandwidth is fitted in the device's own integer domain, never in Q: one
    raw step is worth 0.60 in Q near Q=9.6 and 0.002 near Q=0.5, so rounding
    in Q is wildly non-uniform. ``bw_raw_for_q`` rounds bandwidth *up*, so the
    achieved Q never exceeds the request -- a narrower filter than asked for is
    the more surprising failure.
    """
    if band.kind is not FilterType.PEAKING:
        raise UnsupportedRequest(
            f"only peaking bands are supported; got {band.kind.value}. Shelf "
            f"and pass filter types map to the device's shf_db/type fields, "
            f"whose encodings have never been observed being written."
        )
    freq = int(round(band.freq_hz))
    if not 1 <= freq <= 20_000:
        raise UnsupportedRequest(f"band frequency {band.freq_hz} Hz out of range")
    return EqBand(
        freq=freq,
        level=gain_raw_for(band.gain_dbfs),
        bw=bw_raw_for_q(band.q),
        shf_db=stored.shf_db,
        type=stored.type,
    )


def _eq_to_band(block: EqBand) -> Biquad:
    return Biquad(
        freq_hz=float(block.freq),
        gain_dbfs=gain_dbfs(block.level),
        q=q_from_bw_raw(block.bw),
        kind=FilterType.PEAKING,
        enabled=block.level != FLAT_LEVEL_RAW,
    )


@dataclass
class Dsp408Spp(DspBackend):
    """RFCOMM control of a DSP-408, in engineering units.

    Wrap an already-constructed :class:`~tuner.dsp.device.Dsp408Device` so the
    transport, session and journal are the caller's to configure -- and so this
    class stays pure translation, testable against the in-process fake.
    """

    device: Dsp408Device
    peq_policy: PeqPolicy

    #: Move a parameter by the smallest available step when a planned block
    #: cannot be transmitted at all. On by default because ~1 write in 272
    #: hits a zero checksum, so refusing instead would make roughly a third of
    #: full tunes unloadable. Every adjustment is recorded on the
    #: :class:`PlannedBlock`; set False to get a hard refusal instead.
    nudge_unsendable: bool = True
    _limits: DeviceLimits = field(default_factory=DeviceLimits)
    _identity: DeviceIdentity | None = field(default=None, repr=False)

    # -- lifecycle ---------------------------------------------------------

    @property
    def identity(self) -> DeviceIdentity:
        """What the handshake learned: firmware, preset names, system blocks.

        Raises rather than returning ``None``, because every caller wants it
        for provenance and a silent ``None`` there records a snapshot with no
        firmware string -- which is exactly the sort of missing field that
        makes two measurements look comparable when they are not.
        """
        if self._identity is None:
            raise BackendError("not connected; identity comes from the handshake")
        return self._identity

    @property
    def limits(self) -> DeviceLimits:
        """Resource ceilings.

        Carries the **measured** two-chip delay model as of 2026-08-11: the
        ADAU at ``0x37`` drives outputs 1-4 and the one at ``0x35`` drives
        5-8, so delay RAM is pooled per chip.

        Still missing a program-space field, and the pool *sizes* are
        placeholders, so ``measured`` remains False. The grouping is known;
        the capacities are not.
        """
        return self._limits

    def connect(self) -> None:
        self.device.session.open()
        identity = self.device.session.handshake()
        self._identity = identity

    def disconnect(self) -> None:
        self.device.session.close()

    # -- records -----------------------------------------------------------

    def record(self, output: int) -> bytes:
        """The whole 296-byte channel record.

        Provided because :class:`ChannelConfig` is lossy. Anything that needs
        the complete truth -- a snapshot, a diff, a restore -- uses this, not
        the config view.
        """
        return self.device.record(output)

    def input_mix(self, output: int) -> OutputMix:
        """Which inputs feed ``output``, and at what level.

        Read-only and deliberately outside :class:`ChannelConfig`: the mixer is
        **routing, not tuning**, and nothing in the optimizer should be able to
        change which inputs reach a driver. It is carried through
        read-modify-write like every other undecoded byte was, and now it can
        also be asked a question.
        """
        _require_output(output)
        return OutputMix.decode(
            _block(self.device.record(output), OutputBlock.MIX_IN_1_8)
        )

    def outputs_reachable_from(self, input_index: int) -> tuple[int, ...]:
        """Outputs a stimulus on ``input_index`` (0-based) can actually reach.

        The bench rig drives one input and cables one output at a time, so an
        output the stimulus never reaches measures **silence**. That is caught
        today by ``require_signal_response`` -- correctly, but only after the
        sweep has run and only as "the path is dead", which is the same message
        a cabling fault gives. Asking the device first distinguishes them and
        costs nothing.
        """
        return tuple(
            o
            for o in range(self.limits.n_outputs)
            if self.input_mix(o).reaches(input_index)
        )

    def read_channel(self, output: int) -> ChannelConfig:
        """Current configuration, as far as ``ChannelConfig`` can express it."""
        _require_output(output)
        record = self.device.refresh(output)
        misc = OutputMisc.decode(_block(record, OutputBlock.MISC))
        xover = OutputXover.decode(_block(record, OutputBlock.XOVER))

        bands = []
        for i in range(ADDRESSABLE_BANDS):
            block = EqBand.decode(_block(record, i))
            if block.level != FLAT_LEVEL_RAW:
                bands.append(_eq_to_band(block))

        return ChannelConfig(
            gain_dbfs=gain_dbfs(misc.gain_raw),
            delay_samples=misc.delay_raw,
            crossover=Crossover(
                high_pass_hz=float(xover.h_freq) or None,
                low_pass_hz=float(xover.l_freq) or None,
                # Reported from the stored slope byte rather than invented.
                # The mapping is unconfirmed, so this is a passthrough.
                slope_db_oct=_slope_from_level(xover.h_level),
            ),
            peq=tuple(bands),
            muted=not misc.enabled,
        )

    def set_muted(self, output: int, muted: bool) -> tuple[int, ...]:
        """Mute or unmute one output. Returns the outputs actually written.

        Separate from :meth:`write_channel` because muting is not part of a
        tune: it is how a measurement isolates one driver, and it happens
        between sweeps, many times per run. Going through ``write_channel``
        would rewrite gain, delay, crossover and every band to change one
        byte, on a device where every write is immediately non-volatile.

        A no-op when the channel is already in the requested state -- the
        underlying read-modify-write skips a write that would change nothing,
        so switching isolation from one channel to the next costs two writes,
        not sixteen.

        **Mirrored across link partners.** The device does not mirror; the
        vendor app does, by sending two writes. Muting one half of a linked
        pair would leave the device disagreeing with the model, and the pair
        visibly inconsistent in the app.
        """
        _require_output(output)
        want = 0 if muted else 1

        def mutate(block: bytes) -> bytes:
            stored = OutputMisc.decode(block)
            return replace(stored, enabled=want).encode()

        return self.device.modify_block_mirrored(
            output,
            int(OutputBlock.MISC),
            mutate,
            reason="mute" if muted else "unmute",
        )

    def tuning_digest(self, output: int) -> str:
        """Hash of exactly the fields this backend writes as a tune.

        Gain, delay, both crossover halves and all 31 EQ blocks. Deliberately
        **not** the whole record: ``spk_type``, polarity, channel name, mix and
        link group legitimately differ between two outputs that carry the same
        tune, and mute is the isolator's rather than the tune's.

        Exists so a caller can assert that a gang's members really do hold the
        same correction, without needing to know which bytes those are. Two
        subwoofers in one enclosure must match, and a comparison of whole
        records would fail on ``spk_type`` alone -- which is exactly the sort
        of false alarm that gets a check deleted.

        **Bands sitting at 0 dB contribute nothing but their absence.** A flat
        band is inaudible whatever its frequency and bandwidth, and the vendor
        app seeds each channel's unused slots with its own default layout, so
        hashing them verbatim made two outputs holding the identical audible
        tune disagree. That was found by a test asserting a ganged run leaves
        both subwoofers matched, which is the check this method exists for.
        """
        _require_output(output)
        record = self.device.refresh(output)
        misc = OutputMisc.decode(_block(record, OutputBlock.MISC))
        material = [
            f"gain={misc.gain_raw}".encode(),
            f"delay={misc.delay_raw}".encode(),
            _block(record, OutputBlock.XOVER),
        ]
        for index in range(ADDRESSABLE_BANDS):
            band = EqBand.decode(_block(record, index))
            if band.level == FLAT_LEVEL_RAW:
                continue
            material.append(
                f"{index}:{band.freq}:{band.level}:{band.bw}:{band.type}".encode()
            )
        return hashlib.sha256(b"|".join(material)).hexdigest()

    def plan_channel(self, output: int, config: ChannelConfig) -> list[PlannedBlock]:
        """Every block :meth:`write_channel` would send, in order.

        **Transmits nothing.** Splitting the plan from the application is what
        makes a whole-channel write atomic in the only sense available on a
        device with no undo: either every block is sendable and we go, or none
        is sent at all.

        This was not always split. Until 2026-08-11 ``write_channel`` encoded
        and transmitted block by block, and on real device state it wrote two
        blocks of OUT1 and then raised on the third -- a flattened band whose
        frame checksum computes to zero. See
        :meth:`~tuner.dsp.device.Dsp408Device.preflight`.
        """
        _require_output(output)
        record = self.device.refresh(output)
        misc = OutputMisc.decode(_block(record, OutputBlock.MISC))
        xover = OutputXover.decode(_block(record, OutputBlock.XOVER))

        self._reject_unsupported(output, config, misc, xover)
        plan = [
            *self._plan_misc(config, misc),
            *self._plan_xover(config, xover),
            *self._plan_peq(output, config, record),
        ]
        if not self.nudge_unsendable:
            return plan
        return make_sendable(
            plan,
            lambda data_id, payload: _can_send(
                self.device.session, output, data_id, payload
            ),
        )

    def write_channel(self, output: int, config: ChannelConfig) -> None:
        """Apply what this backend can, and raise for anything it cannot.

        Plans the whole channel, proves every block can be transmitted, and
        only then writes. Blocks the device already holds are skipped by
        :meth:`~tuner.dsp.device.Dsp408Device.write_block`.
        """
        plan = self.plan_channel(output, config)
        self.device.preflight(output, [(b.data_id, b.payload) for b in plan])
        for block in plan:
            self.device.write_block(
                output, block.data_id, block.payload, reason=block.reason
            )

    # -- pieces ------------------------------------------------------------

    def _reject_unsupported(
        self,
        output: int,
        config: ChannelConfig,
        misc: OutputMisc,
        xover: OutputXover,
    ) -> None:
        del misc, xover  # slope is now writable; nothing here consults them
        # Crossover slope became writable on 2026-08-09, when fourteen
        # single-control A/Bs in the vendor app mapped h_level/l_level to
        # 6/12/18/24 dB per octave. It raises only if the request is not one of
        # those four -- see level_raw_for_slope, which refuses rather than
        # rounding.
        try:
            level_raw_for_slope(config.crossover.slope_db_oct)
        except ValueError as exc:
            raise UnsupportedRequest(f"output {output + 1}: {exc}") from None

        if len(config.peq) > self.limits.max_peq_per_channel:
            raise UnsupportedRequest(
                f"output {output + 1}: {len(config.peq)} bands requested, "
                f"limit is {self.limits.max_peq_per_channel}. The device "
                f"addresses {ADDRESSABLE_BANDS}, but two ADAU1701s will not "
                f"run that many on eight channels -- addressable is not "
                f"affordable, and the real figure is unmeasured."
            )

    def _plan_misc(
        self, config: ChannelConfig, stored: OutputMisc
    ) -> list[PlannedBlock]:
        target = OutputMisc(
            # 1 = on, 0 = muted. The sense is inverted from the name the
            # decompiled app gives this byte; measured by A/B on 2026-08-09.
            enabled=0 if config.muted else 1,
            polar=stored.polar,
            gain_raw=gain_raw_for(config.gain_dbfs),
            delay_raw=int(config.delay_samples),
            eq_mode=stored.eq_mode,
            spk_type=stored.spk_type,
        )
        return [PlannedBlock(int(OutputBlock.MISC), target.encode(), "gain/delay")]

    def _plan_xover(
        self, config: ChannelConfig, stored: OutputXover
    ) -> list[PlannedBlock]:
        target = OutputXover(
            h_freq=_hz(config.crossover.high_pass_hz, stored.h_freq),
            h_filter=stored.h_filter,
            h_level=level_raw_for_slope(config.crossover.slope_db_oct),
            l_freq=_hz(config.crossover.low_pass_hz, stored.l_freq),
            l_filter=stored.l_filter,
            l_level=level_raw_for_slope(config.crossover.slope_db_oct),
        )
        return [PlannedBlock(int(OutputBlock.XOVER), target.encode(), "crossover")]

    def _plan_peq(
        self, output: int, config: ChannelConfig, record: bytes
    ) -> list[PlannedBlock]:
        planned: list[PlannedBlock] = []
        for index, band in enumerate(config.peq):
            stored = EqBand.decode(_block(record, index))
            # **Refuse to write peaking parameters into a shelf.**
            #
            # `type` was mapped on 2026-08-09 (0 PEQ, 1 low shelf, 2 high
            # shelf) and this refusal exists because of it. Before that the
            # field was carried through blind, which was correct while its
            # meaning was unknown and is a hazard now that it is known: a shelf
            # is not a peaking section, `bw` does not mean what
            # `q_from_bw_raw` says for one, and `optimize.biquad` fits peaking
            # biquads. The device would run a filter nobody modelled, both
            # sides would look reasonable, and the improvement invariant would
            # compare a prediction against it without noticing.
            if stored.type != EqBandType.PEQ:
                raise UnsupportedRequest(
                    f"output {output + 1} band {index + 1} is stored as "
                    f"{EqBandType(stored.type).name}, not PEQ. This backend "
                    f"fits and writes peaking sections only; writing one here "
                    f"would leave the device running a shelf with peaking "
                    f"parameters. Set the band to PEQ in the vendor app, or "
                    f"exclude it from the fit."
                )
            planned.append(
                PlannedBlock(
                    index, _band_to_eq(band, stored).encode(), f"peq band {index}"
                )
            )

        if self.peq_policy is not PeqPolicy.EXCLUSIVE:
            return planned

        for index in range(len(config.peq), ADDRESSABLE_BANDS):
            stored = EqBand.decode(_block(record, index))
            if stored.level == FLAT_LEVEL_RAW:
                continue
            if stored.type != EqBandType.PEQ:
                # Flattening is a level change and leaves the shape alone, so
                # it is safe on a shelf -- but silently neutralising a band the
                # operator deliberately configured is not this backend's call.
                raise UnsupportedRequest(
                    f"output {output + 1} band {index + 1} is a "
                    f"{EqBandType(stored.type).name} and EXCLUSIVE policy "
                    f"would flatten it. Use LEADING, or clear the band in the "
                    f"vendor app if that is what you meant."
                )
            flat = EqBand(
                freq=stored.freq,
                level=FLAT_LEVEL_RAW,
                bw=stored.bw,
                shf_db=stored.shf_db,
                type=stored.type,
            )
            planned.append(PlannedBlock(index, flat.encode(), f"flatten band {index}"))

        return planned


@dataclass(frozen=True)
class PlannedBlock:
    """One block a channel write intends to send. Nothing transmits it."""

    data_id: int
    payload: bytes
    reason: str

    #: Set when :func:`make_sendable` had to move a parameter to escape a
    #: zero checksum. Human-readable, and meant to reach run provenance: the
    #: device is then running something a hair different from what the
    #: optimizer asked for, and that should be on the record even though the
    #: difference is far below anything measurable.
    nudge: str | None = None


#: How far a nudge may move a frequency, in Hz. One hertz at 2.5 kHz is 0.7
#: cents. Ten is seven cents -- still inaudible, and far below the 0.39 dB
#: measurement repeatability floor this project works to.
MAX_FREQ_NUDGE_HZ = 10

#: How far a nudge may move a gain, in raw steps of 0.1 dB. Two steps is
#: 0.2 dB, half the repeatability floor.
MAX_GAIN_NUDGE_RAW = 2


def _nudged_variants(data_id: int, payload: bytes):
    """Payloads to try, nearest first, when ``payload`` cannot be sent.

    One field per block type, chosen as the one whose smallest step is least
    consequential:

    * **EQ band** -- centre frequency. Continuous on this device (measured
      2026-08-08: a 450 Hz corner reads 449.4), so a 1 Hz step is real rather
      than a rounding artifact, and 0.7 cents is inaudible.
    * **Crossover** -- likewise the corner frequency.
    * **Misc** -- gain, in 0.1 dB steps. **Not delay**: one sample at 48 kHz
      is 26 degrees of phase at 3.5 kHz, which is a real change to the thing
      time alignment exists to control. Gain is the safe knob here; delay is
      not, and neither is bandwidth, which is genuinely quantised.
    """
    if 0 <= data_id < ADDRESSABLE_BANDS:
        band = EqBand.decode(payload)
        for delta in _spiral(MAX_FREQ_NUDGE_HZ):
            freq = band.freq + delta
            if freq > 0:
                yield (
                    replace(band, freq=freq).encode(),
                    f"eq freq {band.freq}->{freq} Hz ({delta:+d})",
                )
    elif data_id == int(OutputBlock.XOVER):
        xover = OutputXover.decode(payload)
        for delta in _spiral(MAX_FREQ_NUDGE_HZ):
            if xover.l_freq:
                freq = xover.l_freq + delta
                if freq > 0:
                    yield (
                        replace(xover, l_freq=freq).encode(),
                        f"lpf {xover.l_freq}->{freq} Hz ({delta:+d})",
                    )
            if xover.h_freq:
                freq = xover.h_freq + delta
                if freq > 0:
                    yield (
                        replace(xover, h_freq=freq).encode(),
                        f"hpf {xover.h_freq}->{freq} Hz ({delta:+d})",
                    )
    elif data_id == int(OutputBlock.MISC):
        misc = OutputMisc.decode(payload)
        for delta in _spiral(MAX_GAIN_NUDGE_RAW):
            raw = misc.gain_raw + delta
            if 0 <= raw <= 600:
                yield (
                    replace(misc, gain_raw=raw).encode(),
                    f"gain {misc.gain_raw / 10 - 60:+.1f}->{raw / 10 - 60:+.1f} dB",
                )


def _spiral(limit: int):
    """1, -1, 2, -2, ... so the nearest usable value wins."""
    for step in range(1, limit + 1):
        yield step
        yield -step


def make_sendable(
    plan: Sequence[PlannedBlock], can_send: Callable[[int, bytes], bool]
) -> list[PlannedBlock]:
    """Return ``plan`` with any unsendable block moved the least distance out.

    **Why this is not over-engineering.** The frame checksum is 8 bits, so
    measured over 200 000 random block writes roughly **1 in 272** cannot be
    sent at all. A tune writing 100 blocks across eight channels therefore has
    about a **31 % chance** of containing at least one unsendable block. A
    backend that only refuses is correct and useless: a third of all tunes
    would fail to load, for a reason having nothing to do with audio.

    So the plan is adjusted rather than rejected -- but never silently. Each
    adjustment is recorded on the block, in engineering units, for provenance.
    ``preflight`` still runs afterwards and still refuses if this could not
    find anything, because a nudge that fails must not become a write that
    half-lands.
    """
    out = []
    for block in plan:
        if can_send(block.data_id, block.payload):
            out.append(block)
            continue
        for payload, description in _nudged_variants(block.data_id, block.payload):
            if can_send(block.data_id, payload):
                out.append(replace(block, payload=payload, nudge=description))
                break
        else:
            out.append(block)  # preflight will refuse it, with a full message
    return out


def _can_send(session, output: int, data_id: int, payload: bytes) -> bool:
    """Whether the session could actually put this block on the wire."""
    try:
        session.block_write_frame(output, data_id, payload).encode()
    except UnsendableFrame:
        return False
    return True


def _require_output(output: int) -> None:
    """Validate at this boundary, and signal it the way the ABC's other
    implementation does.

    ``IndexError`` rather than :class:`BackendError`, matching
    :class:`~tuner.dsp.sim.SimulatedDsp`. Without this the lower layer's
    ``DeviceError`` escaped, and callers written against the simulator would
    not have caught it -- which a shared contract test found on its first run.
    """
    if not 0 <= output < N_OUTPUTS:
        raise IndexError(f"output {output} out of range (0..{N_OUTPUTS - 1})")


def _block(record: bytes, index: int) -> bytes:
    return record[index * BLOCK_LEN : (index + 1) * BLOCK_LEN]


def _hz(requested: float | None, stored: int) -> int:
    """A crossover corner in Hz, or the stored value if not specified.

    ``None`` means "leave it alone", not "disable". Disabling a crossover is
    not something the wire format has been observed doing, and a high-pass
    silently vanishing is the failure mode that kills tweeters.
    """
    if requested is None:
        return stored
    value = int(round(requested))
    if not 1 <= value <= 20_000:
        raise UnsupportedRequest(f"crossover {requested} Hz out of range")
    return value


def _slope_from_level(level: int) -> int:
    """Report the stored slope byte as dB/octave.

    Was ``24 if level == 1 else level``, which was wrong in both branches. It
    rested on a docstring that had conflated two tunes: the OUT5 configuration
    that measured as textbook LR4 carries ``l_level = 3``, not 1. Level 1 is
    12 dB/octave. Corrected 2026-08-09 against the A/B matrix.
    """
    return slope_db_per_octave(level)


# ---------------------------------------------------------------------------
# Documented dead end: Bluetooth Low Energy
#
# These constants describe a GATT layout the device really does expose, and
# which this project believed for two days was the control path. An HCI capture
# of the vendor app driving real hardware settled it: 2918 protocol frames over
# RFCOMM, and exactly five packets touching the BLE ATT channel, none of them
# carrying our protocol. The app never uses GATT.
#
# Kept rather than deleted so the hypothesis cannot be re-derived from scratch
# by someone repeating the original scan. Whether this path *also* works is
# untested and not on the critical path.
#
# The general lesson is in docs/STATE.md: a scan says what a device *offers*,
# or what it happened to answer that day. A capture says what the software
# *does*.
#
# How the RFCOMM channel number was established: not from an SDP record, but
# from the capture's RFCOMM DLCI 2 -- 11784 frames on that DLCI against eight
# multiplexer-control frames on DLCI 0. A backend should still discover the
# channel by SDP at connect time and treat anything other than 1 as worth
# stopping for.
# ---------------------------------------------------------------------------

BLE_ADVERTISED_SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
BLE_CONTROL_SERVICE_UUID = "0000fff0-0000-1000-8000-00805f9b34fb"
BLE_NOTIFY_CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
BLE_WRITE_CHAR_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"
BLE_OBSERVED_MTU = 120
