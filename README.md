## hevc_hdr_editor (Python)

Losslessly edit **HDR10 static metadata (MDCV + CLL)** in HEVC (H.265) video streams.
Optionally remove embedded encoder / *“Writing library”* strings from the HEVC bitstream.

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
  Input file (raw HEVC or container)

* `-o, --output`
  Output file
  (`-` allowed only for raw HEVC stdout)

* `-p, --preset`
  HDR color primaries preset:

  * `DisplayP3`
  * `BT2020`

---

## Optional Arguments

### HDR10 Metadata

* `--maxcll <int>`
  Maximum Content Light Level
  *(default: 1000)*

* `--maxfall <int>`
  Maximum Frame-Average Light Level
  *(default: 400)*

* `--maxmdl <float>`
  Max mastering display luminance (nits)
  *(default: 1000.0)*

* `--minmdl <float>`
  Min mastering display luminance (nits)
  *(default: 0.0001)*

* `--write-json <path>`
  Write generated HDR10 metadata to a JSON file (inspection only)

---

### SEI Handling

* `--add-if-missing` *(default)*
  If **no SEI prefix NAL units exist**, insert MDCV + CLL **before the first VCL NAL**.

* `--no-add-if-missing`
  Disable insertion when no SEI prefix NAL units are found (error instead).

---

### Remove “Writing library” / Encoder Strings

These strings are usually stored as **SEI `user_data_unregistered` (payloadType=5)**.

* `--strip-user-data`
  Remove **all** `user_data_unregistered` SEI messages.

* `--strip-user-data-match "<substring>"`
  Remove `user_data_unregistered` **only if** the payload contains the given text
  (UTF-8 search, errors ignored).

> ⚠️ `--strip-user-data-match` **requires an argument** and should normally be used together with `--strip-user-data`.

---

## Examples

### Raw HEVC (HDR10 edit)

```console
python hevc_hdr_editor.py -i video.hevc -o output.hevc -p BT2020 --maxcll 1200 --maxfall 500
```

---

### Raw HEVC + remove encoder string

Remove any embedded *Writing library* / encoder metadata:

```console
python hevc_hdr_editor.py -i video.hevc -o output.hevc -p DisplayP3 --strip-user-data
```

Remove only if it contains a specific string:

```console
python hevc_hdr_editor.py -i video.hevc -o output.hevc -p DisplayP3 --strip-user-data --strip-user-data-match "Tencent-V265"
```

---

### MKV / MP4 / MOV

```console
python hevc_hdr_editor.py -i input.mkv -o output.mkv -p DisplayP3
```

---

## Output Behavior

* HDR10 metadata is **replaced or inserted**
* Video stream is rewritten **losslessly**
* SEI messages are rewritten safely
* Progress is shown from **1% to 100%**
* No re-encoding is performed

---
