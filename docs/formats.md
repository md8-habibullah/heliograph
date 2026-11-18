# Supported formats

Heliograph indexes **249 file extensions**: 121 picture, 65 video and 63 audio. Run `heliograph formats` to see the list and what *this* machine can decode (it reports the FFmpeg codec counts and the GPU).

The extension only decides what is **indexed**. Whether a file displays or plays is decided per file from its real contents (`heliograph info FILE` shows the verdict).

> **Honest status.** The paths below were exercised with the test files listed in the last column. A format that is indexed but whose decoder is missing on your machine shows a placeholder instead of a thumbnail, and the viewer offers "open with another app". Please report files that should work and do not.

## Pictures

| Family | Extensions (examples) | How they are shown | Tested with |
| --- | --- | --- | --- |
| Web and common | jpg jpeg png gif webp avif bmp ico apng svg | Shown directly by the browser. Thumbnails via Pillow (or FFmpeg) | Real JPEG photos (up to 24 MP) and PNG |
| Large JPEG and PNG | any over about 12 MP | A 2400 px preview is shown first, and the original loads when you zoom (`Z` or click) | 24 MP photo: 0.32 s to first view |
| TIFF and friends | tif tiff tga dds qoi pcx ppm pgm pbm sgi | Pillow decodes; the viewer uses a preview | Not tested on real files |
| Apple, Android, camera | heic heif avif avci hif | FFmpeg, then `heif-convert`, then ImageMagick, whichever your machine has | **Not tested on real samples** |
| Camera RAW | cr2 cr3 nef arw dng orf rw2 raf pef srw and about 30 more | ImageMagick, then `dcraw_emu` (LibRaw), at reduced size | **Not tested on real samples** |
| Professional and rare | psd exr jxl jp2 j2k dpx fits djvu xcf kra | ImageMagick or FFmpeg when built with support | `jxl` conversion tested; others unverified |

Camera-generated metadata (EXIF orientation and taken date) is read from Pillow-readable files. Orientation is applied in thumbnails and previews.

## Video

| Family | Extensions | Playback | Tested with |
| --- | --- | --- | --- |
| Web | mp4 m4v webm ogv | Direct for H.264 8-bit, VP8, VP9, AV1, Theora | H.264 MOV and MP4, VP9 WebM |
| QuickTime and phone | mov qt 3gp 3g2 | Direct when H.264; converted when HEVC (iPhone) or ProRes | H.264 MOV direct, ProRes and 4K HEVC transcoded |
| Matroska | mkv mk3d | Direct or remuxed (for example with AC-3 audio) | AC-3 MKV remuxed; seek and audio-track switching verified |
| Legacy and broadcast | avi wmv asf flv mpg mpeg vob ts m2ts mts mxf rm rmvb divx xvid | Remuxed or transcoded | MPEG-2 transcoded; the rest rely on FFmpeg |
| Raw and exotic | dv hevc h264 obu ivf y4m nut gxf | Transcoded | Unverified |

Everything else is treated the same way: probe, decide, play.

Additionally verified in the player: 10-bit H.264 (transcoded), seeking in all three modes, multi-audio-track switching, external `.srt` subtitles (shown as WebVTT), and a corrupt file shows a clear message instead of a spinner.

## Audio

| Family | Extensions | Playback |
| --- | --- | --- |
| Direct | mp3 flac wav ogg oga opus m4a aac | The browser plays the original |
| Converted on the fly | wma ape wv tta dsf dff aiff caf ac3 dts truehd tak mpc and more | FFmpeg to AAC |

Cover art embedded in a file becomes its thumbnail. Title, artist and album are read for MP4/M4A (pure Python) and for everything else via `ffprobe`, and are searchable.

## Ambiguous extensions

Some extensions are also used by non-media files. `.ts` is MPEG transport stream video, but also TypeScript source, so a `.ts` file is indexed only if it starts with the transport-stream sync pattern. Nothing else is guessed: `.ps`, `.rec`, `.json` and similar are ignored.

## Conversion formats

See [conversion.md](conversion.md).

## Adding a format

1. Add the extension to the right list in `src/heliograph/formats.py` (and to `RAW_EXT`, `PIL_EXT` or `BMFF_EXT` if one of the special readers applies).
2. Make sure `tests/test_formats.py` still passes (it checks the breadth and a few kinds).
3. If it needs a new decoder, add it to the chain in `thumbs.generate()` and document it here.
