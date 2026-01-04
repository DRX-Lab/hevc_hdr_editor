## hevc_hdr_editor (Python)

Lossless editor for **HDR10 static metadata (MDCV + CLL)** and **HEVC SPS/VUI signaling**
(Colorimetry, Color Range, and Video Standard).

This tool **does not re-encode video**.
All operations modify **bitstream signaling only**.

---

## Requirements

* **Python 3.9 or newer**
* **ffmpeg / ffprobe** available in `PATH`
  *(required only for MKV / MP4 / MOV / M4V inputs)*

---

## Supported Inputs

### Raw HEVC (Annex-B)

* `.hevc`

### Containers with HEVC video

* `.mkv`
* `.mp4`
* `.mov`
* `.m4v`

---

## Basic Usage

```bash
python hevc_hdr_editor.py -i INPUT -o OUTPUT [options]
```

---

## Recommended Preset Modes

### Force HDR10 signaling (BT.2020 / PQ)

```bash
python hevc_hdr_editor.py -i in.hevc -o out.hevc \
  -S hdr -p 2020 -C 1000 -F 400 -M 1000.0 -m 0.0001
```

**Effect**

* Sets SPS/VUI colorimetry to **BT.2020 / PQ / BT.2020 non-constant**
* Forces HDR10 SEI:

  * MDCV (payloadType 137)
  * CLL (payloadType 144)
* Ensures HDR metadata is present across the entire stream

---

### Force SDR signaling (BT.709)

```bash
python hevc_hdr_editor.py -i in.hevc -o out.hevc -S sdr
```

**Effect**

* Sets SPS/VUI colorimetry to **BT.709 / BT.709 / BT.709**
* Removes all HDR10 SEI metadata

---

### Remove HDR metadata and colorimetry

```bash
python hevc_hdr_editor.py -i in.hevc -o out.hevc -U hdr
```

**Effect**

* Removes MDCV and CLL SEI messages
* Removes SPS/VUI colorimetry
* MediaInfo reports only **Color range**

---

### Remove colorimetry only

```bash
python hevc_hdr_editor.py -i in.hevc -o out.hevc -U sdr
```

**Effect**

* Removes SPS/VUI colorimetry
* Preserves Color range signaling

---

## Common Editing Scenarios

### Edit HDR10 MaxCLL / MaxFALL only

```bash
python hevc_hdr_editor.py -i video.hevc -o out.hevc -p 2020 -C 1200 -F 500
```

* Updates CLL SEI values
* Preserves existing MDCV data if present

---

### Edit HDR10 mastering luminance only

```bash
python hevc_hdr_editor.py -i video.hevc -o out.hevc -p 2020 -M 4000.0 -m 0.0001
```

* Updates MDCV luminance fields
* Preserves primaries and white point

---

### Remove encoder / “Writing library” strings

```bash
python hevc_hdr_editor.py -i video.hevc -o out.hevc -p p3 -u
```

* Removes SEI `user_data_unregistered` (payloadType 5)
* No effect on video quality or HDR metadata

---

### Remove NTSC / PAL labeling (MediaInfo cleanup)

```bash
python hevc_hdr_editor.py -i video.hevc -o out.hevc -s unspec
```

---

### Force Full or Limited range flag

```bash
python hevc_hdr_editor.py -i video.hevc -o out.hevc -r full
```

```bash
python hevc_hdr_editor.py -i video.hevc -o out.hevc -r limited
```

> **Warning**
> This modifies only the SPS flag. Pixel values are not converted.

---

## Container Workflow Examples

### Edit HDR metadata inside MKV

```bash
python hevc_hdr_editor.py -i input.mkv -o output.mkv -S hdr -p 2020 -C 1000 -F 400
```

---

### Clean container HDR metadata without re-encoding

```bash
python hevc_hdr_editor.py -i input.mp4 -o output.mp4 -U hdr
```

---

## Output Behavior

* Video is rewritten **losslessly**
* HDR10 metadata is inserted or replaced via **SEI prefix NAL units**
* SPS/VUI signaling is edited **in-place**
* Raw HEVC input shows **streaming progress**
* Container input uses **ffmpeg extract → edit → remux**

---
