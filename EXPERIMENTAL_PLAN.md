# Experimental Feature Plan

## Architecture Shifts
- Remove "downloads" from UI (only show library)
- Full JS frontend layer (htmx / alpine or custom vanilla)
- Better visual styling (grid layout, album art)
- Inline web playback for the imported library

## Phase 1: Presentation & Cleanup
1. Remove "Downloads" directory viewer / navigation
2. Setup static assets structure for JS/CSS
3. Embed or fetch album art during search results (Qobuz/Tidal API support this)

## Phase 2: Library Playback (Web Player)
1. Expose Beets library via API endpoints `/api/library/...`
2. Build a player interface (Howler.js or native HTML5 `<audio>`)
3. Support serving `.opus` and `.flac` files directly from the `/music` mount

## Phase 3: Client-side Interactivity
1. Switch search and job polling to async queries
2. Persistent player across navigation (requires SPA-like routing or htmx boosting)
