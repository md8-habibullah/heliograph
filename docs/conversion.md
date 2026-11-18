# Converting pictures, video and audio

Select files in the library (or open one in the viewer and press `C`), choose a target, and the jobs run in the background through FFmpeg. From the terminal: `heliograph convert`.

## Safety rules

- **Originals are never changed or overwritten.** Output is written to `<name>.<ext>.part` and renamed when it is complete. If the target name exists, a ` (1)` style suffix is added. An interrupted job leaves no half-finished file with the final name.
- **Only presets, never free-form FFmpeg arguments.** The command line is built from a whitelist of presets and validated options. Source paths are always absolute, so a file named `-foo` cannot be read as an option.
- **One job at a time, at low CPU and disk priority**, so the PC stays usable. Video encoding uses the GPU (VAAPI) when this machine has it and falls back to software on any failure.
- **Progress is real** (read from FFmpeg), jobs can be cancelled, and finished jobs are listed in the jobs panel (the badge on the toolbar).
- Output files get the permissions your normal umask would give them (the service itself is private, but your converted files are yours to share).

## Presets (22)

| Makes | Presets |
| --- | --- |
| Pictures | `img-jpg` `img-png` `img-webp` `img-avif` `img-jxl` `img-tiff` `img-bmp` |
| Video | `vid-mp4-h264` `vid-mp4-hevc` `vid-webm-vp9` `vid-webm-av1` `vid-mkv-copy` `vid-mp4-copy` `vid-mov-prores` `vid-gif` `vid-webp` |
| Audio (from audio or by extracting the sound of a video) | `aud-mp3` `aud-m4a` `aud-opus` `aud-ogg` `aud-flac` `aud-wav` |

`heliograph convert --list` prints them with descriptions. In the terminal you can also give a short name (`--to mp4`, `webm`, `jpg`, `avif`, `mp3`, `flac`, `gif`).

## Options

| Option | Meaning |
| --- | --- |
| Quality: `high`, `balanced`, `small` | Maps to sensible encoder settings per format |
| Max height (video) | 2160, 1440, 1080, 720, 480 or 360. Never upscales |
| Max size (pictures) | Longest side in pixels, up to 20,000 |
| Output folder | Default: next to each original |
| GPU: `auto` or `off` | Hardware encoding where available |
| Strip metadata | Drops EXIF and tags |

A mixed selection converts what fits the preset and reports how many files were skipped.

## Example

```bash
# Phone videos to small, widely playable MP4 files in another folder
heliograph convert --to mp4 --quality small --max-height 720 --out ~/Videos/small ~/Videos/*.mov

# A folder of RAW or HEIC pictures to JPEG, longest side 2000 px, no GPS or camera data
heliograph convert --to jpg --max-size 2000 --strip ~/Pictures/trip/*.heic
```

## What is tested

- The test suite runs **every one of the 22 presets** on a small real input (a PNG, a one-second H.264 clip, a WAV tone) and checks that the result is a file FFmpeg can read back, of the right kind (audio-only for audio presets), within the requested size limit, with no `.part` file left behind.
- Validation of options (unknown presets, bad sizes, a resize on a stream-copy preset, a missing or system output folder) is unit-tested.
- Conversions through the app were also tried by hand: two videos to MP4 (6.9 MB to 2.2 MB) and a photo to WebP.

Not covered by automated tests: cancelling a running job, HEIC and RAW **sources** (they depend on your FFmpeg, ImageMagick and LibRaw builds), and GPU (VAAPI) encoding, which needs a machine with a supported GPU. The software path is always the final fallback.
