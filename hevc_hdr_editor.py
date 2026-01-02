#!/usr/bin/env python3
"""
hevc_hdr_editor.py

Edits HDR10 static metadata in HEVC (H.265) by replacing (or inserting) SEI prefix messages:
- Mastering Display Colour Volume (MDCV / ST 2086)  payloadType = 137
- Content Light Level (CLL / MaxCLL+MaxFALL)       payloadType = 144

Additionally (optional):
- Strip SEI user_data_unregistered (payloadType = 5), typically used to embed "Writing library"/encoder strings.

Supported inputs:
1) Raw HEVC Annex-B elementary streams (start-code delimited)
2) Matroska / MP4 / MOV containers (via ffmpeg extract + remux)

CLI:
  -i/--input   input file (raw .hevc Annex-B or container .mkv/.mp4/.mov/.m4v)
  -o/--output  output file
  -p/--preset  DisplayP3 or BT2020
  --maxcll     default 1000
  --maxfall    default 400
  --maxmdl     default 1000.0 nits
  --minmdl     default 0.0001 nits
  --add-if-missing / --no-add-if-missing
      If enabled (default), and the stream contains *no* SEI prefix NAL units at all,
      the tool will insert MDCV+CLL as two SEI prefix NAL units right before the first
      VCL NAL unit (the first video slice), which is a safe/compatible insertion point.

  --strip-user-data
      Remove SEI user_data_unregistered (payloadType=5) messages found in SEI prefix NAL units.
      This is commonly where encoder/“Writing library” strings live.

  --strip-user-data-match "substring"
      If set, only remove user_data_unregistered messages whose payload contains this substring
      (searched in UTF-8 decoded text with errors ignored). If omitted and --strip-user-data is set,
      removes all payloadType=5 messages.

Behavior:
- SEI Prefix NAL units are rewritten losslessly (nal_unit_type=39).
- SEI messages are always split: each SEI message is written in its own SEI prefix NAL.
- Streaming processing for raw input: does NOT load the entire file into memory (handles multi-GB files).
- Progress is printed to STDERR (1%..100%) based on bytes read.

Performance notes:
- Uses fast start-code scanning via bytearray.find (no per-byte Python loops).
- Uses large read chunks by default for higher throughput on Windows.

Container requirement:
- ffmpeg + ffprobe must be available on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, BinaryIO

# HEVC NAL unit types
NAL_SEI_PREFIX = 39  # prefix SEI
# SEI payload types
SEI_PAYLOAD_USER_DATA_UNREGISTERED = 5
SEI_PAYLOAD_MDCV = 137  # MasteringDisplayColourVolume
SEI_PAYLOAD_CLL = 144   # ContentLightLevel

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


@dataclass
class EditMdcv:
    preset: str
    max_display_mastering_luminance: float  # nits
    min_display_mastering_luminance: float  # nits


@dataclass
class EditCll:
    max_content_light_level: int
    max_average_light_level: int


@dataclass
class EditStrip:
    strip_user_data: bool
    user_data_match: Optional[str]


@dataclass
class EditConfig:
    mdcv: EditMdcv
    cll: EditCll
    add_if_missing: bool
    strip: EditStrip


class CountingReader:
    """
    Wraps a binary file object and counts bytes read via .read().
    This is reliable even when buffering/iterators are involved.
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


def _run(cmd: List[str]) -> None:
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(
            "Command failed:\n"
            f"  {' '.join(cmd)}\n\n"
            f"stdout:\n{p.stdout.decode('utf-8', 'replace')}\n\n"
            f"stderr:\n{p.stderr.decode('utf-8', 'replace')}\n"
        )


def _ffprobe_video_codec(path: str) -> Optional[str]:
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


def _is_container(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext in {".mkv", ".mp4", ".mov", ".m4v"}


def _nal_type(nal_header: bytes) -> int:
    # forbidden_zero_bit(1) + nal_unit_type(6) + ...
    return (nal_header[0] >> 1) & 0x3F


def _is_vcl_nal_type(ntype: int) -> bool:
    # VCL NAL unit types in HEVC are 0..31
    return 0 <= ntype <= 31


def _sei_prefix_nal_header() -> bytes:
    # nal_unit_type = 39 => 0x4E in first byte (39<<1), nuh_layer_id=0, tid_plus1=1
    return b"\x4E\x01"


def _is_rbsp_trailing_bits(rem: bytes) -> bool:
    return len(rem) >= 1 and rem[0] == 0x80 and all(b == 0x00 for b in rem[1:])


def _ebsp_to_rbsp(ebsp: bytes) -> bytes:
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


def _rbsp_to_ebsp(rbsp: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b in (0x00, 0x01, 0x02, 0x03):
            out.append(0x03)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _read_sei_messages(rbsp: bytes) -> List[Tuple[int, bytes]]:
    msgs: List[Tuple[int, bytes]] = []
    i = 0
    n = len(rbsp)
    while i < n:
        if _is_rbsp_trailing_bits(rbsp[i:]):
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


def _encode_sei_messages(messages: List[Tuple[int, bytes]]) -> bytes:
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


def _make_single_sei_prefix_nal(start_code_len: int, payload_type: int, payload: bytes) -> bytes:
    start_code = b"\x00\x00\x01" if start_code_len == 3 else b"\x00\x00\x00\x01"
    nal_header = _sei_prefix_nal_header()
    rbsp = _encode_sei_messages([(payload_type, payload)])
    ebsp = _rbsp_to_ebsp(rbsp)
    return start_code + nal_header + ebsp


def _parse_mdcv(payload: bytes) -> Dict[str, object]:
    """
    MDCV payload layout (interleaved, as commonly displayed by MediaInfo):
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


def _encode_mdcv(fields: Dict[str, object]) -> bytes:
    dp_x = list(fields["display_primaries_x"])
    dp_y = list(fields["display_primaries_y"])
    wp = list(fields["white_point"])
    max_u = int(fields["max_display_mastering_luminance_units"])
    min_u = int(fields["min_display_mastering_luminance_units"])
    prim = [dp_x[0], dp_y[0], dp_x[1], dp_y[1], dp_x[2], dp_y[2]]  # interleaved
    return struct.pack(">6H2H2I", *(prim + wp + [max_u, min_u]))


def _encode_cll(max_cll: int, max_fall: int) -> bytes:
    return struct.pack(">2H", max_cll & 0xFFFF, max_fall & 0xFFFF)


def _default_mdcv_fields(preset: str) -> Dict[str, object]:
    p = PRESET_PRIMARIES[preset]
    return {
        "display_primaries_x": list(p["display_primaries_x"]),
        "display_primaries_y": list(p["display_primaries_y"]),
        "white_point": list(p["white_point"]),
        "max_display_mastering_luminance_units": int(round(1000.0 * MDL_FACTOR)),
        "min_display_mastering_luminance_units": int(round(0.0001 * MDL_FACTOR)),
    }


def _apply_mdcv(existing_payload: Optional[bytes], cfg: EditMdcv) -> bytes:
    fields = _parse_mdcv(existing_payload) if existing_payload else _default_mdcv_fields(cfg.preset)

    p = PRESET_PRIMARIES[cfg.preset]
    fields["display_primaries_x"] = list(p["display_primaries_x"])
    fields["display_primaries_y"] = list(p["display_primaries_y"])
    fields["white_point"] = list(p["white_point"])

    fields["max_display_mastering_luminance_units"] = int(round(cfg.max_display_mastering_luminance * MDL_FACTOR))
    fields["min_display_mastering_luminance_units"] = int(round(cfg.min_display_mastering_luminance * MDL_FACTOR))
    return _encode_mdcv(fields)


def _apply_cll(cfg: EditCll) -> bytes:
    return _encode_cll(cfg.max_content_light_level, cfg.max_average_light_level)


def _print_progress(percent: int, bar_width: int = 46) -> None:
    filled = int(round((percent / 100.0) * bar_width))
    bar = "■" * filled + " " * (bar_width - filled)
    sys.stderr.write(f"\r[{bar}] {percent:.1f}%")
    sys.stderr.flush()
    if percent >= 100:
        sys.stderr.write("\n")
        sys.stderr.flush()


_START3 = b"\x00\x00\x01"


def _find_next_start_code(buf: bytearray, start: int) -> Optional[Tuple[int, int]]:
    """
    Fast search for the next Annex-B start code using bytearray.find.
    Returns (pos, length) where length is 3 or 4.
    """
    pos = buf.find(_START3, start)
    if pos == -1:
        return None
    if pos > 0 and buf[pos - 1] == 0x00:
        return (pos - 1, 4)
    return (pos, 3)


def _iter_annexb_nals_stream(f: BinaryIO, chunk_size: int = 64 * 1024 * 1024):
    """
    Streaming Annex-B NAL iterator.
    Yields tuples: (start_code_len, nal_unit_without_start_code)
    """
    buf = bytearray()
    eof = False
    offset = 0

    # Fill until first start code
    while True:
        sc = _find_next_start_code(buf, offset)
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

    # Iterate NALs
    while True:
        # Determine current start code length
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

        # Find next start code after current one
        next_sc = _find_next_start_code(buf, offset + cur_sc_len)
        while next_sc is None and not eof:
            chunk = f.read(chunk_size)
            if not chunk:
                eof = True
                break
            buf.extend(chunk)
            next_sc = _find_next_start_code(buf, offset + cur_sc_len)

        if next_sc is None and eof:
            # Last NAL
            nal = bytes(buf[offset + cur_sc_len:])
            yield (cur_sc_len, nal)
            return

        next_pos, _ = next_sc
        nal = bytes(buf[offset + cur_sc_len:next_pos])
        yield (cur_sc_len, nal)

        # Advance offset to next_pos
        offset = next_pos

        # Trim buffer occasionally to avoid unbounded growth
        if offset > 8 * 1024 * 1024:
            del buf[:offset]
            offset = 0


def _process_one_nal(sc_len: int, nal: bytes, cfg: EditConfig, state: Dict[str, bool]) -> bytes:
    """
    Returns bytes to write for this NAL, including its start code.
    'state' tracks whether MDCV/CLL have been output at least once (global insertion).
    """
    start_code = b"\x00\x00\x01" if sc_len == 3 else b"\x00\x00\x00\x01"

    if len(nal) < 2:
        return start_code + nal

    ntype = _nal_type(nal[:2])
    if ntype != NAL_SEI_PREFIX:
        return start_code + nal

    state["seen_sei_prefix"] = True

    nal_header = nal[:2]
    rbsp = _ebsp_to_rbsp(nal[2:])
    messages = _read_sei_messages(rbsp)

    if not messages:
        return start_code + nal

    had_mdcv = any(t == SEI_PAYLOAD_MDCV for t, _ in messages)
    had_cll = any(t == SEI_PAYLOAD_CLL for t, _ in messages)

    existing_mdcv = next((p for t, p in messages if t == SEI_PAYLOAD_MDCV), None)

    out = bytearray()

    # Split: one SEI message per SEI prefix NAL (with optional stripping of payloadType=5)
    for t, p in messages:
        if cfg.strip.strip_user_data and t == SEI_PAYLOAD_USER_DATA_UNREGISTERED:
            if cfg.strip.user_data_match is None:
                continue
            try:
                txt = p.decode("utf-8", "ignore")
            except Exception:
                txt = ""
            if cfg.strip.user_data_match in txt:
                continue

        if t == SEI_PAYLOAD_MDCV:
            payload = _apply_mdcv(p, cfg.mdcv)
            state["mdcv"] = True
            one = [(SEI_PAYLOAD_MDCV, payload)]
        elif t == SEI_PAYLOAD_CLL:
            payload = _apply_cll(cfg.cll)
            state["cll"] = True
            one = [(SEI_PAYLOAD_CLL, payload)]
        else:
            one = [(t, p)]

        new_rbsp = _encode_sei_messages(one)
        new_ebsp = _rbsp_to_ebsp(new_rbsp)
        out += start_code + nal_header + new_ebsp

    # Insert missing MDCV/CLL right after this SEI prefix only once globally
    if (not state["mdcv"]) and (not had_mdcv):
        payload = _apply_mdcv(existing_mdcv, cfg.mdcv)
        out += start_code + nal_header + _rbsp_to_ebsp(_encode_sei_messages([(SEI_PAYLOAD_MDCV, payload)]))
        state["mdcv"] = True

    if (not state["cll"]) and (not had_cll):
        payload = _apply_cll(cfg.cll)
        out += start_code + nal_header + _rbsp_to_ebsp(_encode_sei_messages([(SEI_PAYLOAD_CLL, payload)]))
        state["cll"] = True

    # If everything got stripped and nothing was inserted, remove this SEI prefix NAL completely.
    if len(out) == 0:
        return b""

    return bytes(out)


def _process_raw_streaming(input_path: str, output_path: str, cfg: EditConfig) -> None:
    total_size: Optional[int] = None
    if input_path != "-" and os.path.exists(input_path):
        try:
            total_size = os.path.getsize(input_path)
        except OSError:
            total_size = None

    # state flags
    state = {
        "mdcv": False,
        "cll": False,
        "seen_sei_prefix": False,
        "inserted_before_vcl": False,
    }
    last_percent = -1
    reader: Optional[CountingReader] = None

    if total_size is not None:
        _print_progress(0)

    # Use large chunks for Windows throughput
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
        for sc_len, nal in _iter_annexb_nals_stream(in_f, chunk_size=chunk_size):
            # progress
            if total_size is not None and input_path != "-":
                bytes_read = reader.bytes_read if reader is not None else 0
                percent = int((bytes_read * 100) / total_size) if total_size > 0 else 100
                if percent > 100:
                    percent = 100
                if percent != last_percent:
                    _print_progress(percent)
                    last_percent = percent

            # If stream has *no* SEI prefix NALs at all, optionally insert right before first VCL.
            if cfg.add_if_missing and (not state["seen_sei_prefix"]) and (not state["inserted_before_vcl"]) and len(nal) >= 2:
                ntype = _nal_type(nal[:2])
                if _is_vcl_nal_type(ntype):
                    # Insert both SEIs before this first slice NAL.
                    mdcv_payload = _apply_mdcv(None, cfg.mdcv)
                    cll_payload = _apply_cll(cfg.cll)
                    out_f.write(_make_single_sei_prefix_nal(sc_len, SEI_PAYLOAD_MDCV, mdcv_payload))
                    out_f.write(_make_single_sei_prefix_nal(sc_len, SEI_PAYLOAD_CLL, cll_payload))
                    state["mdcv"] = True
                    state["cll"] = True
                    state["inserted_before_vcl"] = True

            out_f.write(_process_one_nal(sc_len, nal, cfg, state))

        # If we never saw a SEI prefix NAL and we did not insert before VCL, decide what to do.
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
            _print_progress(100)

    finally:
        if input_path != "-":
            in_f.close()
        if output_path != "-":
            out_f.close()


def _process_container(input_path: str, output_path: str, cfg: EditConfig) -> None:
    codec = _ffprobe_video_codec(input_path)
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
        _run([
            "ffmpeg", "-y", "-v", "error",
            "-i", input_path,
            "-map", "0:v:0",
            "-c", "copy",
            "-bsf:v", "hevc_mp4toannexb",
            "-f", "hevc",
            extracted,
        ])

        sys.stderr.write("Editing HDR10 SEI metadata...\n")
        sys.stderr.flush()
        _process_raw_streaming(extracted, edited, cfg)

        sys.stderr.write("Remuxing container with edited video stream...\n")
        sys.stderr.flush()
        _run([
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


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Edit HDR10 metadata (MDCV+CLL) in HEVC. No JSON config required."
    )
    parser.add_argument("-i", "--input", required=True, help="Input file: raw .hevc (Annex-B) or container (.mkv/.mp4/.mov/.m4v). Use '-' for stdin (raw only).")
    parser.add_argument("-o", "--output", required=True, help="Output file. Use '-' for stdout (raw only).")
    parser.add_argument("-p", "--preset", required=True, choices=["DisplayP3", "BT2020"], help='Preset to use: "DisplayP3" or "BT2020".')
    parser.add_argument("--maxcll", type=int, default=1000, help="MaxCLL value (default: 1000).")
    parser.add_argument("--maxfall", type=int, default=400, help="MaxFALL value (default: 400).")
    parser.add_argument("--maxmdl", type=float, default=1000.0, help="Max mastering display luminance in nits (default: 1000).")
    parser.add_argument("--minmdl", type=float, default=0.0001, help="Min mastering display luminance in nits (default: 0.0001).")
    parser.add_argument("--write-json", default=None, help="If set, writes the generated HDR10 JSON metadata to this path (for inspection).")

    add_group = parser.add_mutually_exclusive_group()
    add_group.add_argument(
        "--add-if-missing",
        dest="add_if_missing",
        action="store_true",
        help="If the stream has no SEI prefix NALs, insert MDCV+CLL before the first VCL NAL (default).",
    )
    add_group.add_argument(
        "--no-add-if-missing",
        dest="add_if_missing",
        action="store_false",
        help="Disable insertion when a stream has no SEI prefix NALs (old behavior: error).",
    )
    parser.set_defaults(add_if_missing=True)

    parser.add_argument(
        "--strip-user-data",
        action="store_true",
        help="Remove SEI user_data_unregistered (payloadType=5) messages from SEI prefix NAL units.",
    )
    parser.add_argument(
        "--strip-user-data-match",
        default=None,
        help="If set, only remove user_data_unregistered messages whose payload contains this UTF-8 substring "
             "(e.g. 'Tencent-V265' or 'x265'). If omitted and --strip-user-data is set, removes all payloadType=5.",
    )

    return parser


def _build_cfg_from_args(args: argparse.Namespace) -> EditConfig:
    mdcv = EditMdcv(
        preset=args.preset,
        max_display_mastering_luminance=float(args.maxmdl),
        min_display_mastering_luminance=float(args.minmdl),
    )
    cll = EditCll(
        max_content_light_level=int(args.maxcll),
        max_average_light_level=int(args.maxfall),
    )
    strip = EditStrip(
        strip_user_data=bool(getattr(args, "strip_user_data", False)),
        user_data_match=getattr(args, "strip_user_data_match", None),
    )
    return EditConfig(mdcv=mdcv, cll=cll, add_if_missing=bool(args.add_if_missing), strip=strip)


def _maybe_write_json(args: argparse.Namespace) -> None:
    if not args.write_json:
        return
    prim = PRESET_PRIMARIES[args.preset]
    cfg = {
        "mdcv": {
            "preset": args.preset,
            "primaries": {
                "display_primaries_x": list(prim["display_primaries_x"]),
                "display_primaries_y": list(prim["display_primaries_y"]),
                "white_point": list(prim["white_point"]),
            },
            "max_display_mastering_luminance": float(args.maxmdl),
            "min_display_mastering_luminance": float(args.minmdl),
        },
        "cll": {
            "max_content_light_level": int(args.maxcll),
            "max_average_light_level": int(args.maxfall),
        },
        "add_if_missing": bool(args.add_if_missing),
        "strip": {
            "strip_user_data": bool(getattr(args, "strip_user_data", False)),
            "user_data_match": getattr(args, "strip_user_data_match", None),
        },
    }
    out_path = os.path.abspath(args.write_json)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4)
    sys.stderr.write(f"JSON created: {out_path}\n")
    sys.stderr.flush()


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    cfg = _build_cfg_from_args(args)
    _maybe_write_json(args)

    if args.input == "-":
        _process_raw_streaming("-", args.output, cfg)
        return 0

    if _is_container(args.input):
        _process_container(args.input, args.output, cfg)
    else:
        _process_raw_streaming(args.input, args.output, cfg)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
