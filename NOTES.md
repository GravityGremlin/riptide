# Riptide Notes

## Opus Conversion — Multi-Channel Audio

Opus supports stereo, 5.1, and 7.1 surround natively. The issue is ffmpeg channel layout mapping, not Opus itself.

### Problem
M4A files with `5.1(side)` channel layout fail with:
```
Invalid channel layout 5.1(side) for specified mapping family -1
```

### Solution
Use `-ch_layout 5.1` (or `7.1`) to force standard layout before encoding:
```bash
ffmpeg -y -i input.m4a -c:a libopus -b:a 160k -ch_layout 5.1 output.opus
```

### Verified
- `-ch_layout 5.1` produces 6-channel Opus correctly
- `-ac 6` also works as shorthand
- Default (no flag) fails on non-standard layouts like `5.1(side)`

### Conversion Command (recommended)
```bash
# Detect channel count and set layout accordingly
channels=$(ffprobe -v error -show_entries stream=channels -of csv=p=0 input.m4a)
case $channels in
  1) layout="mono" ;;
  2) layout="stereo" ;;
  6) layout="5.1" ;;
  8) layout="7.1" ;;
  *) layout="stereo" ;;  # fallback: downmix
esac
ffmpeg -y -i input.m4a -c:a libopus -b:a 160k -ch_layout $layout output.opus
```

### When to Keep FLAC
- If the source is already FLAC and multi-channel, and the player chain supports it, keeping FLAC avoids generation loss
- For web playback (Funkwhale/Jellyfin/Navidrome), Opus 5.1 works fine
- For archival, FLAC is always safe

### Incident: Blind Guardian (2026-06-21)
10 M4A files from `Blind Guardian/Somewhere Far Beyond Revisited (2024)` were deleted by a conversion script that created 0-byte Opus files (ffmpeg failed but file check passed). Original M4A files lost. Need re-download from Qobuz.
