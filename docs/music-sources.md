# Free music for Cine Kahani

"Free to download" is not the same as "safe to monetize". The pipeline only uses tracks listed in
`music/licenses.yaml` with an allowed license, and it adds required credits to the video description for you.

## Sources that usually work
| Source | License to log | Notes |
|---|---|---|
| Pixabay Music | `pixabay` | No credit needed. Read their current license page once. Content ID claims can still happen. |
| Free Music Archive | `cc0` or `cc-by` | Filter by license. Never take NC (non-commercial) or ND (no derivatives) tracks. |
| Freesound (music loops) | `cc0` | Only CC0 items. |
| Incompetech (Kevin MacLeod) | `cc-by` | Needs the credit line he specifies; pass it with `--credit`. |
| Tracks you make or buy | `own` / `royalty-free` | Keep the receipt. |

YouTube's Audio Library cannot be bulk downloaded by a program and its terms limit use to YouTube, so add
those tracks by hand if you use them (`royalty-free`).

## Add a track
```
cinekahani music add ~/Downloads/calm-night.mp3 --license cc0 --source https://example.org/track --mood calm
cinekahani music add song.mp3 --license cc-by --source URL --title "Song" --artist "Name" --credit "Music: Song by Name (CC BY 4.0)"
cinekahani music list
```
Moods the pipeline understands: calm, upbeat, tense, dramatic, mysterious. NC/ND licenses are refused.
