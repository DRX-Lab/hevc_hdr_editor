#!/usr/bin/env python3
"""
hevc_hdr_editor.py

Edits HDR10 static metadata in HEVC (H.265) by replacing (or inserting) SEI prefix messages:
- Mastering Display Colour Volume (MDCV / SMPTE ST 2086) payloadType = 137
- Content Light Level (CLL / MaxCLL + MaxFALL)          payloadType = 144

Also supports (optional) SPS/VUI edits that affect what MediaInfo shows as:
- "Standard : NTSC"          -> SPS VUI video_format (3 bits)
- "Color range : Limited/Full" -> SPS VUI video_full_range_flag (1 bit)

Also supports (optional) removal of encoder strings:
- Strip SEI user_data_unregistered (payloadType = 5), often used for "Writing library"/encoder tags.

Supported inputs:
1) Raw HEVC Annex-B elementary streams (start-code delimited)
2) Matroska / MP4 / MOV containers (via ffmpeg extract + remux)

CLI (short + long options):
  -i / --input        Input file (raw .hevc Annex-B OR container .mkv/.mp4/.mov/.m4v). Use '-' for stdin (raw only).
  -o / --output       Output file. Use '-' for stdout (raw only).

  -p / --preset       HDR primaries preset: p3 or 2020
                      (Used when writing/replacing MDCV primaries + white point.)

  -C / --maxcll       MaxCLL value (nits) for CLL SEI (payloadType=144)
  -F / --maxfall      MaxFALL value (nits) for CLL SEI (payloadType=144)
  -M / --maxmdl       Max mastering display luminance (nits) for MDCV SEI (payloadType=137)
  -m / --minmdl       Min mastering display luminance (nits) for MDCV SEI (payloadType=137)

  -a / --add-if-missing
  -A / --no-add-if-missing
      If enabled (default), and the stream contains *no* SEI prefix NAL units at all,
      insert MDCV+CLL as two SEI prefix NAL units right before the first VCL NAL unit.

  -u / --strip-user-data
      Remove SEI user_data_unregistered (payloadType=5) messages from SEI prefix NAL units
      (commonly encoder / "Writing library" strings).

  -s / --standard     component|pal|ntsc|secam|mac|unspec
      Set SPS VUI video_format (MediaInfo "Standard"). Use "unspec" to remove "Standard : NTSC".

  -r / --range        limited|full
      Set SPS VUI video_full_range_flag (MediaInfo "Color range").

Notes / Safety:
- This tool performs signaling edits. Changing "Color range" flag does NOT convert pixel levels.
- Streaming processing for raw input: does NOT load the entire file into memory (handles multi-GB files).
- Progress is printed to STDERR (1%..100%) based on bytes read (raw input only).

Container requirement:
- ffmpeg + ffprobe must be available on PATH.
"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, BinaryIO

# ----------------------------
# Constants / Tables
# ----------------------------

# HEVC NAL unit types
NAL_SEI_PREFIX = 39  # prefix SEI
NAL_SPS = 33         # SPS

# SEI payload types
SEI_PAYLOAD_USER_DATA_UNREGISTERED = 5
SEI_PAYLOAD_MDCV = 137  # MasteringDisplayColourVolume (ST 2086)
SEI_PAYLOAD_CLL = 144   # ContentLightLevel (MaxCLL/MaxFALL)

D65_WHITEPOINT = (15635, 16450)
MDL_FACTOR = 10000.0  # nits -> units of 0.0001 nits

# Presets
PRESET_PRIMARIES: Dict[str, Dict[str, Tuple[int, ...]]] = {
    "DisplayP3": {
        "display_primaries_x": (34000, 13250, 7500),
        "display_primaries_y": (16000, 34500, 3000),
        "white_point": D65_WHITEPOINT,
    },
    "BT2020": {
        "display_primaries_x": (35400, 8500, 6550),
        "display_primaries_y": (14600, 39850, 2300),
        "white_point": D65_WHITEPOINT,
    },
    "BT709": {
        "display_primaries_x": (32000, 15000, 7500),
        "display_primaries_y": (16500, 30000, 3000),
        "white_point": D65_WHITEPOINT,
    },
}

# SPS VUI mappings (MediaInfo)
VIDEO_FORMAT_MAP = {
    0: "component",
    1: "pal",
    2: "ntsc",
    3: "secam",
    4: "mac",
    5: "unspec",
}
VIDEO_FORMAT_INV = {v: k for k, v in VIDEO_FORMAT_MAP.items()}

RANGE_INV = {"limited": 0, "full": 1}

_START3 = b"\x00\x00\x01"


# ----------------------------
# Dataclasses
# ----------------------------

@dataclass
class EditMdcv:
    preset: str
    max_display_mastering_luminance: Optional[float]  # nits (None => do not override)
    min_display_mastering_luminance: Optional[float]  # nits (None => do not override)


@dataclass
class EditCll:
    max_content_light_level: Optional[int]   # None => do not override
    max_average_light_level: Optional[int]   # None => do not override


@dataclass
class EditStrip:
    strip_user_data: bool


@dataclass
class EditSpsVui:
    standard: Optional[int]      # video_format (0..7)
    full_range: Optional[int]    # video_full_range_flag (0/1)


@dataclass
class EditConfig:
    mdcv: EditMdcv
    cll: EditCll
    add_if_missing: bool
    strip: EditStrip
    sps_vui: EditSpsVui


class CountingReader:
    """
    Wraps a binary file object and counts bytes read via .read().
    Reliable even with buffering.
    """
    def __init__(self, f: BinaryIO):
        self._f = f
        self.bytes_read = 0

    def read(self, n: int = -1) -> bytes:
        data = self._f.read(n)
        if data:
            self.bytes_read += len(data)
        return data

    def close(self) -> None:
        self._f.close()

    def tell(self) -> int:
        try:
            return self._f.tell()
        except Exception:
            return self.bytes_read


# ----------------------------
# External tools (ffmpeg/ffprobe)
# ----------------------------

def run_cmd(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"  {' '.join(cmd)}\n\n"
            f"stdout:\n{p.stdout.decode('utf-8', 'replace')}\n\n"
            f"stderr:\n{p.stderr.decode('utf-8', 'replace')}\n"
        )


def ffprobe_video_codec(path: str) -> Optional[str]:
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=nw=1:nk=1",
        path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        return None
    codec = p.stdout.decode("utf-8", "replace").strip()
    return codec or None


def is_container(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in {".mkv", ".mp4", ".mov", ".m4v"}


# ----------------------------
# NAL helpers
# ----------------------------

def nal_type(nal_header: bytes) -> int:
    # forbidden_zero_bit(1) + nal_unit_type(6) + ...
    return (nal_header[0] >> 1) & 0x3F


def is_vcl_nal_type(ntype: int) -> bool:
    # VCL NAL unit types in HEVC are 0..31
    return 0 <= ntype <= 31


def sei_prefix_nal_header() -> bytes:
    # nal_unit_type = 39 => 0x4E in first byte (39<<1), nuh_layer_id=0, tid_plus1=1
    return b"\x4E\x01"


def is_rbsp_trailing_bits(rem: bytes) -> bool:
    return len(rem) >= 1 and rem[0] == 0x80 and all(b == 0x00 for b in rem[1:])


def ebsp_to_rbsp(ebsp: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    i = 0
    while i < len(ebsp):
        b = ebsp[i]
        if zeros >= 2 and b == 0x03:
            i += 1
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
        i += 1
    return bytes(out)


def rbsp_to_ebsp(rbsp: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b in (0x00, 0x01, 0x02, 0x03):
            out.append(0x03)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


# ----------------------------
# SEI helpers
# ----------------------------

def read_sei_messages(rbsp: bytes) -> List[Tuple[int, bytes]]:
    msgs: List[Tuple[int, bytes]] = []
    i = 0
    n = len(rbsp)
    while i < n:
        if is_rbsp_trailing_bits(rbsp[i:]):
            break

        payload_type = 0
        while i < n and rbsp[i] == 0xFF:
            payload_type += 255
            i += 1
        if i >= n:
            break
        payload_type += rbsp[i]
        i += 1

        payload_size = 0
        while i < n and rbsp[i] == 0xFF:
            payload_size += 255
            i += 1
        if i >= n:
            break
        payload_size += rbsp[i]
        i += 1

        if i + payload_size > n:
            break
        payload = rbsp[i:i + payload_size]
        i += payload_size
        msgs.append((payload_type, payload))
    return msgs


def encode_sei_messages(messages: List[Tuple[int, bytes]]) -> bytes:
    rbsp = bytearray()
    for payload_type, payload in messages:
        pt = payload_type
        while pt >= 255:
            rbsp.append(0xFF)
            pt -= 255
        rbsp.append(pt & 0xFF)

        ps = len(payload)
        while ps >= 255:
            rbsp.append(0xFF)
            ps -= 255
        rbsp.append(ps & 0xFF)

        rbsp.extend(payload)

    rbsp.append(0x80)  # rbsp_trailing_bits
    return bytes(rbsp)


def make_single_sei_prefix_nal(start_code_len: int, payload_type: int, payload: bytes) -> bytes:
    start_code = b"\x00\x00\x01" if start_code_len == 3 else b"\x00\x00\x00\x01"
    nal_header = sei_prefix_nal_header()
    rbsp = encode_sei_messages([(payload_type, payload)])
    ebsp = rbsp_to_ebsp(rbsp)
    return start_code + nal_header + ebsp


# ----------------------------
# HDR payload builders
# ----------------------------

def parse_mdcv(payload: bytes) -> Dict[str, object]:
    """
    MDCV payload layout (interleaved):
        xR, yR, xG, yG, xB, yB,
        white_point_x, white_point_y,
        max_luminance, min_luminance
    """
    if len(payload) < 20:
        raise ValueError("MDCV payload too short")
    off = 0
    prim = list(struct.unpack_from(">6H", payload, off)); off += 12
    dp_x = [prim[0], prim[2], prim[4]]
    dp_y = [prim[1], prim[3], prim[5]]
    wp_x, wp_y = struct.unpack_from(">2H", payload, off); off += 4
    max_mdl, min_mdl = struct.unpack_from(">2I", payload, off); off += 8
    return {
        "display_primaries_x": dp_x,
        "display_primaries_y": dp_y,
        "white_point": [wp_x, wp_y],
        "max_display_mastering_luminance_units": max_mdl,
        "min_display_mastering_luminance_units": min_mdl,
    }


def encode_mdcv(fields: Dict[str, object]) -> bytes:
    dp_x = list(fields["display_primaries_x"])
    dp_y = list(fields["display_primaries_y"])
    wp = list(fields["white_point"])
    max_u = int(fields["max_display_mastering_luminance_units"])
    min_u = int(fields["min_display_mastering_luminance_units"])
    prim = [dp_x[0], dp_y[0], dp_x[1], dp_y[1], dp_x[2], dp_y[2]]
    return struct.pack(">6H2H2I", *(prim + wp + [max_u, min_u]))


def encode_cll(max_cll: int, max_fall: int) -> bytes:
    return struct.pack(">2H", max_cll & 0xFFFF, max_fall & 0xFFFF)


def default_mdcv_fields(preset: str) -> Dict[str, object]:
    p = PRESET_PRIMARIES[preset]
    return {
        "display_primaries_x": list(p["display_primaries_x"]),
        "display_primaries_y": list(p["display_primaries_y"]),
        "white_point": list(p["white_point"]),
        "max_display_mastering_luminance_units": int(round(1000.0 * MDL_FACTOR)),
        "min_display_mastering_luminance_units": int(round(0.0001 * MDL_FACTOR)),
    }


def apply_mdcv(existing_payload: Optional[bytes], cfg: EditMdcv) -> bytes:
    # Start from existing if present, else from preset defaults
    fields = parse_mdcv(existing_payload) if existing_payload else default_mdcv_fields(cfg.preset)

    # Always set primaries/whitepoint to preset
    p = PRESET_PRIMARIES[cfg.preset]
    fields["display_primaries_x"] = list(p["display_primaries_x"])
    fields["display_primaries_y"] = list(p["display_primaries_y"])
    fields["white_point"] = list(p["white_point"])

    # Override luminance only if provided
    if cfg.max_display_mastering_luminance is not None:
        fields["max_display_mastering_luminance_units"] = int(round(cfg.max_display_mastering_luminance * MDL_FACTOR))
    if cfg.min_display_mastering_luminance is not None:
        fields["min_display_mastering_luminance_units"] = int(round(cfg.min_display_mastering_luminance * MDL_FACTOR))

    return encode_mdcv(fields)


def apply_cll(existing_payload: Optional[bytes], cfg: EditCll) -> bytes:
    # If user did not provide both values, preserve existing if possible
    if cfg.max_content_light_level is None or cfg.max_average_light_level is None:
        if existing_payload is not None and len(existing_payload) >= 4:
            return existing_payload[:4]
        # If missing and user didn't provide, insert 0/0 (valid but meaningless)
        return encode_cll(0, 0)

    return encode_cll(cfg.max_content_light_level, cfg.max_average_light_level)


# ----------------------------
# Progress
# ----------------------------

def print_progress(percent: int, bar_width: int = 46) -> None:
    filled = int(round((percent / 100.0) * bar_width))
    bar = "■" * filled + " " * (bar_width - filled)
    sys.stderr.write(f"\r[{bar}] {percent:.1f}%")
    sys.stderr.flush()
    if percent >= 100:
        sys.stderr.write("\n")
        sys.stderr.flush()


# ----------------------------
# Annex-B iterator
# ----------------------------

def find_next_start_code(buf: bytearray, start: int) -> Optional[Tuple[int, int]]:
    pos = buf.find(_START3, start)
    if pos == -1:
        return None
    if pos > 0 and buf[pos - 1] == 0x00:
        return (pos - 1, 4)
    return (pos, 3)


def iter_annexb_nals_stream(f: BinaryIO, chunk_size: int = 64 * 1024 * 1024):
    """
    Streaming Annex-B NAL iterator.
    Yields: (start_code_len, nal_without_start_code)
    """
    buf = bytearray()
    eof = False
    offset = 0

    while True:
        sc = find_next_start_code(buf, offset)
        if sc:
            pos, _ = sc
            if pos > 0:
                del buf[:pos]
            offset = 0
            break
        if eof:
            return
        chunk = f.read(chunk_size)
        if not chunk:
            eof = True
        else:
            buf.extend(chunk)

    while True:
        if len(buf) - offset < 4:
            if eof:
                return
            chunk = f.read(chunk_size)
            if not chunk:
                eof = True
            else:
                buf.extend(chunk)
            continue

        cur_sc_len = 4 if buf[offset:offset + 4] == b"\x00\x00\x00\x01" else 3

        next_sc = find_next_start_code(buf, offset + cur_sc_len)
        while next_sc is None and not eof:
            chunk = f.read(chunk_size)
            if not chunk:
                eof = True
                break
            buf.extend(chunk)
            next_sc = find_next_start_code(buf, offset + cur_sc_len)

        if next_sc is None and eof:
            nal = bytes(buf[offset + cur_sc_len:])
            yield (cur_sc_len, nal)
            return

        next_pos, _ = next_sc
        nal = bytes(buf[offset + cur_sc_len:next_pos])
        yield (cur_sc_len, nal)

        offset = next_pos
        if offset > 8 * 1024 * 1024:
            del buf[:offset]
            offset = 0


# ----------------------------
# SPS/VUI bit editing (Standard / Color range)
# ----------------------------

class BitReader:
    __slots__ = ("data", "bitpos", "size_bits")

    def __init__(self, data: bytes):
        self.data = data
        self.bitpos = 0
        self.size_bits = len(data) * 8

    def read_bits(self, n: int) -> int:
        if n == 0:
            return 0
        if self.bitpos + n > self.size_bits:
            raise ValueError("BitReader: out of data")
        val = 0
        for _ in range(n):
            byte_i = self.bitpos >> 3
            bit_i = 7 - (self.bitpos & 7)
            val = (val << 1) | ((self.data[byte_i] >> bit_i) & 1)
            self.bitpos += 1
        return val

    def read_bool(self) -> int:
        return self.read_bits(1)

    def read_ue(self) -> int:
        zeros = 0
        while True:
            b = self.read_bits(1)
            if b == 0:
                zeros += 1
            else:
                break
        if zeros == 0:
            return 0
        info = self.read_bits(zeros)
        return (1 << zeros) - 1 + info


def skip_profile_tier_level(br: BitReader, max_sub_layers_minus1: int) -> None:
    br.read_bits(2); br.read_bits(1); br.read_bits(5)
    br.read_bits(32); br.read_bits(48); br.read_bits(8)

    sub_layer_profile_present_flag = [0] * max_sub_layers_minus1
    sub_layer_level_present_flag = [0] * max_sub_layers_minus1
    for i in range(max_sub_layers_minus1):
        sub_layer_profile_present_flag[i] = br.read_bool()
        sub_layer_level_present_flag[i] = br.read_bool()

    if max_sub_layers_minus1 > 0:
        for _ in range(8 - max_sub_layers_minus1):
            br.read_bits(2)

    for i in range(max_sub_layers_minus1):
        if sub_layer_profile_present_flag[i]:
            br.read_bits(2); br.read_bits(1); br.read_bits(5)
            br.read_bits(32); br.read_bits(48)
        if sub_layer_level_present_flag[i]:
            br.read_bits(8)


def skip_short_term_ref_pic_set(br: BitReader, st_rps_idx: int, num_delta_pocs: List[int]) -> None:
    inter_pred = 0
    if st_rps_idx != 0:
        inter_pred = br.read_bool()

    if inter_pred:
        delta_idx_minus1 = br.read_ue()
        ref_rps_idx = st_rps_idx - (delta_idx_minus1 + 1)
        if ref_rps_idx < 0 or ref_rps_idx >= st_rps_idx:
            raise ValueError("Invalid ref_rps_idx while skipping RPS")

        br.read_bool()  # delta_rps_sign
        br.read_ue()    # abs_delta_rps_minus1
        ndp = num_delta_pocs[ref_rps_idx]
        for _ in range(ndp + 1):
            used = br.read_bool()
            if not used:
                br.read_bool()
        num_delta_pocs.append(ndp)
    else:
        num_negative = br.read_ue()
        num_positive = br.read_ue()
        for _ in range(num_negative):
            br.read_ue(); br.read_bool()
        for _ in range(num_positive):
            br.read_ue(); br.read_bool()
        num_delta_pocs.append(num_negative + num_positive)


@dataclass
class VuiLoc:
    ok: bool
    vf_bitpos: Optional[int] = None
    vf_val: Optional[int] = None
    vfr_bitpos: Optional[int] = None
    vfr_val: Optional[int] = None


def locate_vui_bits_in_sps(rbsp: bytes) -> VuiLoc:
    br = BitReader(rbsp)

    br.read_bits(4)
    max_sub_layers_minus1 = br.read_bits(3)
    br.read_bits(1)

    skip_profile_tier_level(br, max_sub_layers_minus1)

    br.read_ue()
    chroma_format_idc = br.read_ue()
    if chroma_format_idc == 3:
        br.read_bits(1)

    br.read_ue(); br.read_ue()

    if br.read_bool():
        br.read_ue(); br.read_ue(); br.read_ue(); br.read_ue()

    br.read_ue(); br.read_ue(); br.read_ue()

    sps_sub_layer_ordering_info_present_flag = br.read_bool()
    start_i = 0 if sps_sub_layer_ordering_info_present_flag else max_sub_layers_minus1
    for _ in range(start_i, max_sub_layers_minus1 + 1):
        br.read_ue(); br.read_ue(); br.read_ue()

    br.read_ue(); br.read_ue()
    br.read_ue(); br.read_ue()
    br.read_ue(); br.read_ue()

    if br.read_bool():
        if br.read_bool():
            raise ValueError("scaling_list_data_present_flag=1 not supported for SPS editing.")

    br.read_bool()
    br.read_bool()

    if br.read_bool():
        br.read_bits(4); br.read_bits(4)
        br.read_ue(); br.read_ue()
        br.read_bool()

    num_st_rps = br.read_ue()
    num_delta_pocs: List[int] = []
    for idx in range(num_st_rps):
        skip_short_term_ref_pic_set(br, idx, num_delta_pocs)

    if br.read_bool():
        raise ValueError("long_term_ref_pics_present_flag=1 not supported for SPS editing.")

    br.read_bool()
    br.read_bool()

    vui_present = bool(br.read_bool())
    if not vui_present:
        return VuiLoc(ok=False)

    if br.read_bool():
        aspect_ratio_idc = br.read_bits(8)
        if aspect_ratio_idc == 255:
            br.read_bits(16); br.read_bits(16)

    if br.read_bool():
        br.read_bool()

    video_signal_type_present = bool(br.read_bool())
    if not video_signal_type_present:
        return VuiLoc(ok=False)

    vf_bitpos = br.bitpos
    vf_val = br.read_bits(3)

    vfr_bitpos = br.bitpos
    vfr_val = br.read_bool()

    return VuiLoc(ok=True, vf_bitpos=vf_bitpos, vf_val=vf_val, vfr_bitpos=vfr_bitpos, vfr_val=vfr_val)


def set_bits(rbsp: bytearray, bitpos: int, nbits: int, value: int) -> None:
    for i in range(nbits):
        bit = (value >> (nbits - 1 - i)) & 1
        p = bitpos + i
        byte_i = p >> 3
        bit_i = 7 - (p & 7)
        mask = 1 << bit_i
        if bit:
            rbsp[byte_i] |= mask
        else:
            rbsp[byte_i] &= (~mask) & 0xFF


def edit_sps_vui_if_requested(nal: bytes, cfg: EditConfig) -> bytes:
    if (cfg.sps_vui.standard is None) and (cfg.sps_vui.full_range is None):
        return nal

    if len(nal) < 2:
        return nal

    if nal_type(nal[:2]) != NAL_SPS:
        return nal

    hdr = nal[:2]
    rbsp = ebsp_to_rbsp(nal[2:])

    try:
        loc = locate_vui_bits_in_sps(rbsp)
    except Exception:
        # Unsupported SPS features: do not touch
        return nal

    if not loc.ok:
        return nal

    rb = bytearray(rbsp)
    changed = False

    if cfg.sps_vui.standard is not None and loc.vf_bitpos is not None and loc.vf_val is not None:
        if loc.vf_val != cfg.sps_vui.standard:
            set_bits(rb, loc.vf_bitpos, 3, cfg.sps_vui.standard)
            changed = True

    if cfg.sps_vui.full_range is not None and loc.vfr_bitpos is not None and loc.vfr_val is not None:
        if loc.vfr_val != cfg.sps_vui.full_range:
            set_bits(rb, loc.vfr_bitpos, 1, cfg.sps_vui.full_range)
            changed = True

    if not changed:
        return nal

    return hdr + rbsp_to_ebsp(bytes(rb))


# ----------------------------
# Core NAL processing
# ----------------------------

def process_one_nal(sc_len: int, nal: bytes, cfg: EditConfig, state: Dict[str, bool]) -> bytes:
    """
    Returns bytes to write for this NAL, including its start code.
    Applies optional SPS edits (Standard/Range) and SEI HDR edits.

    IMPORTANT FIX:
    - MDCV/CLL insertion is done per SEI prefix NAL, not globally once per stream.
      This prevents HDR metadata from "disappearing" mid-stream when later SEI prefix NALs
      lack MDCV/CLL payloads.
    """
    start_code = b"\x00\x00\x01" if sc_len == 3 else b"\x00\x00\x00\x01"

    if len(nal) < 2:
        return start_code + nal

    # Optional SPS edits
    if nal_type(nal[:2]) == NAL_SPS:
        nal2 = edit_sps_vui_if_requested(nal, cfg)
        return start_code + nal2

    ntype = nal_type(nal[:2])
    if ntype != NAL_SEI_PREFIX:
        return start_code + nal

    state["seen_sei_prefix"] = True

    nal_header = nal[:2]
    rbsp = ebsp_to_rbsp(nal[2:])
    messages = read_sei_messages(rbsp)

    if not messages:
        return start_code + nal

    had_mdcv = any(t == SEI_PAYLOAD_MDCV for t, _ in messages)
    had_cll = any(t == SEI_PAYLOAD_CLL for t, _ in messages)

    existing_mdcv = next((p for t, p in messages if t == SEI_PAYLOAD_MDCV), None)
    existing_cll = next((p for t, p in messages if t == SEI_PAYLOAD_CLL), None)

    out = bytearray()

    # Split: one SEI message per SEI prefix NAL
    for t, p in messages:
        # Optional strip of user_data_unregistered (encoder strings)
        if cfg.strip.strip_user_data and t == SEI_PAYLOAD_USER_DATA_UNREGISTERED:
            continue

        if t == SEI_PAYLOAD_MDCV:
            payload = apply_mdcv(p, cfg.mdcv)
            one = [(SEI_PAYLOAD_MDCV, payload)]
        elif t == SEI_PAYLOAD_CLL:
            payload = apply_cll(p, cfg.cll)
            one = [(SEI_PAYLOAD_CLL, payload)]
        else:
            one = [(t, p)]

        new_rbsp = encode_sei_messages(one)
        new_ebsp = rbsp_to_ebsp(new_rbsp)
        out += start_code + nal_header + new_ebsp

    # FIX: Ensure MDCV/CLL presence for THIS SEI prefix NAL whenever missing.
    # This is no longer "only once globally".
    if not had_mdcv:
        payload = apply_mdcv(existing_mdcv, cfg.mdcv)
        out += start_code + nal_header + rbsp_to_ebsp(encode_sei_messages([(SEI_PAYLOAD_MDCV, payload)]))

    if not had_cll:
        payload = apply_cll(existing_cll, cfg.cll)
        out += start_code + nal_header + rbsp_to_ebsp(encode_sei_messages([(SEI_PAYLOAD_CLL, payload)]))

    if len(out) == 0:
        return b""

    return bytes(out)


# ----------------------------
# Raw processing (streaming)
# ----------------------------

def process_raw_streaming(input_path: str, output_path: str, cfg: EditConfig) -> None:
    total_size: Optional[int] = None
    if input_path != "-" and os.path.exists(input_path):
        try:
            total_size = os.path.getsize(input_path)
        except OSError:
            total_size = None

    state = {
        "seen_sei_prefix": False,
        "inserted_before_vcl": False,
    }
    last_percent = -1
    reader: Optional[CountingReader] = None

    if total_size is not None:
        print_progress(0)

    chunk_size = 64 * 1024 * 1024

    if input_path == "-":
        in_f = sys.stdin.buffer
    else:
        base_f = open(input_path, "rb")
        reader = CountingReader(base_f)
        in_f = reader  # type: ignore[assignment]

    if output_path == "-":
        out_f = sys.stdout.buffer
    else:
        out_f = open(output_path, "wb")

    try:
        for sc_len, nal in iter_annexb_nals_stream(in_f, chunk_size=chunk_size):
            if total_size is not None and input_path != "-":
                bytes_read = reader.bytes_read if reader is not None else 0
                percent = int((bytes_read * 100) / total_size) if total_size > 0 else 100
                if percent > 100:
                    percent = 100
                if percent != last_percent:
                    print_progress(percent)
                    last_percent = percent

            # If the stream has no SEI prefix NALs at all, optionally insert before first VCL.
            if cfg.add_if_missing and (not state["seen_sei_prefix"]) and (not state["inserted_before_vcl"]) and len(nal) >= 2:
                ntype = nal_type(nal[:2])
                if is_vcl_nal_type(ntype):
                    mdcv_payload = apply_mdcv(None, cfg.mdcv)
                    cll_payload = apply_cll(None, cfg.cll)
                    out_f.write(make_single_sei_prefix_nal(sc_len, SEI_PAYLOAD_MDCV, mdcv_payload))
                    out_f.write(make_single_sei_prefix_nal(sc_len, SEI_PAYLOAD_CLL, cll_payload))
                    state["inserted_before_vcl"] = True

            out_f.write(process_one_nal(sc_len, nal, cfg, state))

        if not state["seen_sei_prefix"] and not state["inserted_before_vcl"]:
            if cfg.add_if_missing:
                raise RuntimeError(
                    "No VCL NAL units were found to anchor insertion (unexpected/invalid stream). "
                    "HDR metadata could not be inserted."
                )
            raise RuntimeError(
                "No SEI Prefix NAL units were found, so HDR metadata could not be inserted. "
                "Re-run with --add-if-missing to insert MDCV/CLL before the first VCL NAL."
            )

        if total_size is not None and last_percent < 100:
            print_progress(100)

    finally:
        if input_path != "-":
            in_f.close()
        if output_path != "-":
            out_f.close()


# ----------------------------
# Container processing (ffmpeg extract + remux)
# ----------------------------

def process_container(input_path: str, output_path: str, cfg: EditConfig) -> None:
    codec = ffprobe_video_codec(input_path)
    if codec is None:
        raise RuntimeError("ffprobe could not read the input container.")
    if codec.lower() not in {"hevc", "h265"}:
        raise RuntimeError(f"Video codec is {codec!r}; expected HEVC.")

    if output_path == "-":
        raise RuntimeError("Container output to stdout is not supported. Please provide -o <file>.")

    with tempfile.TemporaryDirectory(prefix="hevc_hdr_editor_") as td:
        extracted = os.path.join(td, "video.hevc")
        edited = os.path.join(td, "video_edited.hevc")

        sys.stderr.write("Extracting HEVC bitstream with ffmpeg...\n")
        sys.stderr.flush()
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-i", input_path,
            "-map", "0:v:0",
            "-c", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc",
            extracted,
        ])

        sys.stderr.write("Editing HEVC bitstream (HDR SEI + SPS VUI)...\n")
        sys.stderr.flush()
        process_raw_streaming(extracted, edited, cfg)

        sys.stderr.write("Remuxing container with edited video stream...\n")
        sys.stderr.flush()
        run_cmd([
            "ffmpeg", "-y", "-v", "error",
            "-i", input_path,
            "-i", edited,
            "-map", "0",
            "-map", "-0:v:0",
            "-map", "1:v:0",
            "-c", "copy",
            "-map_metadata", "0",
            output_path,
        ])


# ----------------------------
# CLI / Config
# ----------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Edit HDR10 SEI (MDCV+CLL) and optional SPS VUI (Standard/Color range) in HEVC."
    )

    parser.add_argument(
        "-i", "--input", required=True,
        help="Input file: raw .hevc (Annex-B) or container (.mkv/.mp4/.mov/.m4v). Use '-' for stdin (raw only)."
    )
    parser.add_argument(
        "-o", "--output", required=True,
        help="Output file. Use '-' for stdout (raw only)."
    )

    parser.add_argument(
        "-p", "--preset", required=True, choices=["p3", "2020"],
        help='HDR primaries preset: "p3" (Display P3 D65) or "2020" (BT.2020 D65).'
    )

    parser.add_argument("-C", "--maxcll", type=int, default=None, help="MaxCLL value for HDR CLL (payloadType=144).")
    parser.add_argument("-F", "--maxfall", type=int, default=None, help="MaxFALL value for HDR CLL (payloadType=144).")
    parser.add_argument("-M", "--maxmdl", type=float, default=None, help="Max mastering display luminance (nits) for MDCV (payloadType=137).")
    parser.add_argument("-m", "--minmdl", type=float, default=None, help="Min mastering display luminance (nits) for MDCV (payloadType=137).")

    add_group = parser.add_mutually_exclusive_group()
    add_group.add_argument(
        "-a", "--add-if-missing", dest="add_if_missing", action="store_true",
        help="If the stream has no SEI prefix NALs, insert MDCV+CLL before the first VCL NAL."
    )
    add_group.add_argument(
        "-A", "--no-add-if-missing", dest="add_if_missing", action="store_false",
        help="Disable insertion when a stream has no SEI prefix NALs."
    )
    parser.set_defaults(add_if_missing=True)

    parser.add_argument(
        "-u", "--strip-user-data", action="store_true",
        help="Remove SEI user_data_unregistered (payloadType=5) messages from SEI prefix NAL units."
    )

    # SPS VUI
    parser.add_argument(
        "-s", "--standard", default=None,
        choices=["component", "pal", "ntsc", "secam", "mac", "unspec"],
        help="Set SPS VUI video_format (MediaInfo 'Standard'). Use 'unspec' to remove NTSC/PAL labeling."
    )
    parser.add_argument(
        "-r", "--range", default=None, choices=["limited", "full"],
        help="Set SPS VUI video_full_range_flag (MediaInfo 'Color range')."
    )

    return parser


def build_cfg_from_args(args: argparse.Namespace) -> EditConfig:
    preset_name = "DisplayP3" if args.preset == "p3" else "BT2020"

    mdcv = EditMdcv(
        preset=preset_name,
        max_display_mastering_luminance=args.maxmdl,
        min_display_mastering_luminance=args.minmdl,
    )
    cll = EditCll(
        max_content_light_level=args.maxcll,
        max_average_light_level=args.maxfall,
    )
    strip = EditStrip(strip_user_data=bool(args.strip_user_data))

    sps_standard = VIDEO_FORMAT_INV[args.standard] if args.standard is not None else None
    sps_range = RANGE_INV[args.range] if args.range is not None else None
    sps_vui = EditSpsVui(standard=sps_standard, full_range=sps_range)

    return EditConfig(
        mdcv=mdcv,
        cll=cll,
        add_if_missing=bool(args.add_if_missing),
        strip=strip,
        sps_vui=sps_vui,
    )


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    cfg = build_cfg_from_args(args)

    if args.input == "-":
        process_raw_streaming("-", args.output, cfg)
        return 0

    if is_container(args.input):
        process_container(args.input, args.output, cfg)
    else:
        process_raw_streaming(args.input, args.output, cfg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
