# Deepwork: Webamp Persistent Overlay + Interface Overhaul

## Oracle-Reviewed Architecture
- **htmx** `hx-boost="true"` `<body>` for SPA transitions
- **Webamp** `<script>` `<head>` (survives body swap)
- **Player container** `hx-preserve` attribute (survives DOM replacement)
- **`#content-area`** swap target all page content

## Implementation Phases

### Phase 1: layout.html Complete Rewrite
**Files**: `app/templates/layout.html`
**Changes**:
- Move Webamp script `<head>` unpkg CDN
- Add `<div id="webamp-player" hx-preserve>` fixed bottom page
- Initialize Webamp global scope (persists across navigation)
- `hx-boost="true"` `<body>` SPA transitions
- Bottom player bar: current track info + expand/collapse Webamp
- Global functions: `playAlbum(artist, album)`, `playTrack(url, artist, title, album)`
- CSS bottom player bar fixed position

### Phase 2: Template Play Buttons
**Files**: `browse.html`, `results.html`
**Changes**:
- "Play Winamp" button browse.html calls `playAlbum()` instead linking `/player`
- Play button each track row calls `playTrack()`
- Play button each album card results.html calls `playAlbum()`
- Remove old `audio` elements browse.html (replaced Webamp)

### Phase 3: Backend Cleanup
**Files**: `app/main.py`
**Changes**:
- Remove `/player` route
- Add CORS headers `/api/library` response

### Phase 4: Oracle Review + Final Validation
- Oracle reviews implementation
- Fix issues
- End-to-end test verify
