## hevc_hdr_editor (Python)

Losslessly edit **HDR10 static metadata (MDCV + CLL)** and optional **SPS/VUI signaling** in HEVC (H.265) video streams.

Supports:

* Editing / inserting **MDCV (ST 2086)** and **CLL (MaxCLL/MaxFALL)** in **SEI prefix** NAL units
* Editing SPS VUI flags that affect MediaInfo display:

  * **Standard : NTSC/PAL/...** (SPS VUI `video_format`)
  * **Color range : Limited/Full** (SPS VUI `video_full_range_flag`)
* Optionally removing embedded encoder / *“Writing library”* strings (**SEI user_data_unregistered**, payloadType=5)

---

## Requirements

* **Python 3.9+**
* **ffmpeg / ffprobe** available in `PATH`
  (required **only** when processing MKV / MP4 / MOV containers)

---

## Supported Inputs

* **Raw HEVC Annex-B**

  * `.hevc`
* **Containers with HEVC video**

  * `.mkv`
  * `.mp4`
  * `.mov`
  * `.m4v`

---

## Usage

```console
python hevc_hdr_editor.py -i INPUT -o OUTPUT -p PRESET [options]
```

---

## Required Arguments

* `-i, --input`
  Input file (raw HEVC or container). Use `-` for stdin (**raw HEVC only**).

* `-o, --output`
  Output file. Use `-` for stdout (**raw HEVC only**).

* `-p, --preset`
  HDR primaries preset (short names):

  * `p3`    (Display P3 D65)
  * `2020`  (BT.2020 D65)

> Note: The preset is used when writing/replacing **MDCV primaries + white point**.

---

## Optional Arguments

### HDR10 Metadata (SEI)

These options **override only what you provide** (no enforced defaults).

* `-C, --maxcll <int>`
  Set **MaxCLL** (CLL SEI payloadType=144).

* `-F, --maxfall <int>`
  Set **MaxFALL** (CLL SEI payloadType=144).

* `-M, --maxmdl <float>`
  Set **max mastering display luminance** in nits (MDCV SEI payloadType=137).

* `-m, --minmdl <float>`
  Set **min mastering display luminance** in nits (MDCV SEI payloadType=137).

---

### SEI Handling

* `-a, --add-if-missing` *(default)*
  If **no SEI prefix NAL units exist**, insert MDCV + CLL **before the first VCL NAL**.

* `-A, --no-add-if-missing`
  Disable insertion when no SEI prefix NAL units are found.

---

### Remove “Writing library” / Encoder Strings

These strings are usually stored as **SEI `user_data_unregistered` (payloadType=5)**.

* `-u, --strip-user-data`
  Remove **all** `user_data_unregistered` SEI messages (payloadType=5).

---

### SPS/VUI Signaling (MediaInfo “Standard” and “Color range”)

These options modify **SPS VUI** signaling bits (they do **not** convert pixel values).

* `-s, --standard <component|pal|ntsc|secam|mac|unspec>`
  Set SPS VUI `video_format` (MediaInfo: **Standard**).
  Use `unspec` to remove “Standard : NTSC” labeling.

* `-r, --range <limited|full>`
  Set SPS VUI `video_full_range_flag` (MediaInfo: **Color range**).

> Warning: Changing `--range` only changes the *flag*. If the actual signal levels are not consistent, players may render incorrectly.

---

## Examples

### Raw HEVC: remove “Standard : NTSC” and force Full range flag

```console
python hevc_hdr_editor.py -i video.hevc -o out.hevc -p 2020 -s unspec -r full
```

---

### Raw HEVC: edit HDR10 CLL values

```console
python hevc_hdr_editor.py -i video.hevc -o out.hevc -p 2020 -C 1200 -F 500
```

---

### Raw HEVC: remove encoder / “Writing library” strings

```console
python hevc_hdr_editor.py -i video.hevc -o out.hevc -p p3 -u
```

---

### MKV / MP4 / MOV (container workflow)

```console
python hevc_hdr_editor.py -i input.mkv -o output.mkv -p p3 -s unspec
```

---

## Output Behavior

* Video is rewritten **losslessly** (no re-encoding)
* HDR10 metadata (MDCV/CLL) is **replaced or inserted** via SEI prefix NAL units
* SPS VUI flags (Standard/Color range) are edited **in-place** when requested
* Progress is shown from **1% to 100%** for raw inputs
* Container inputs are processed via **ffmpeg extract → edit → remux**

---
