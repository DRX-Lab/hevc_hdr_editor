## hevc_hdr_editor (Python)

Losslessly edit **HDR10 metadata (MDCV + CLL)** in HEVC (H.265) files.

---

## Requirements

* **Python 3.9+**
* **ffmpeg / ffprobe** in PATH (only for MKV/MP4/MOV)

---

## Supported inputs

* Raw HEVC Annex-B (`.hevc`)
* Containers with HEVC video:

  * `.mkv`, `.mp4`, `.mov`, `.m4v`

---

## Usage

```console
python hevc_hdr_editor.py -i INPUT -o OUTPUT -p PRESET [options]
```

### Required

* `-i, --input`  Input file
* `-o, --output` Output file
* `-p, --preset` `DisplayP3` or `BT2020`

### Optional

* `--maxcll <int>`   (default: `1000`)
* `--maxfall <int>`  (default: `400`)
* `--maxmdl <float>` (default: `1000.0`)
* `--minmdl <float>` (default: `0.0001`)
* `--write-json <path>` Write generated HDR10 JSON (optional)

---

## Examples

### Raw HEVC

```console
python hevc_hdr_editor.py -i video.hevc -o output.hevc -p BT2020 --maxcll 1200 --maxfall 500
```

### MKV / MP4

```console
python hevc_hdr_editor.py -i input.mkv -o output.mkv -p DisplayP3
```

---

## Output

* HDR10 metadata is replaced or inserted
* Video stream is rewritten **losslessly**
* Progress shown from **1% to 100%**

---
