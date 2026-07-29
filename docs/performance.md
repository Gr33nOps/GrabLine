# Performance

Measured on one Linux laptop with a real X display during the 1.22.0 pass.
Absolute numbers move between machines; the deltas are the point.

Re-run:

```sh
python scripts/perf/bench.py all        # startup + idle
```

## Before / after

| What | Before | After | |
|---|---|---|---|
| Startup to first paint | 1741 ms | 885 ms | −49% |
| Idle, Downloads page | 12.5% CPU · 134 wakeups/s | 1.2% · 7/s | −90% |
| Idle, Dashboard page | 13.0% CPU · 132 wakeups/s | 1.2% · 6/s | −91% |
| Idle, hidden to tray | 12.8% CPU · 133 wakeups/s | 1.3% · 9/s | −90% |
| 5 downloads running | 27.5% CPU | 17.1% CPU | −38% |
| RSS at rest | 183 MB | 172 MB | −6% |

Startup is a median of warm runs; idle is a 6-second window taken after the
background extractor warm-up settles; the download figure is five concurrent
transfers from a local server over a 12-second window.

## What was actually wrong

**The app animated things that were not moving.** The 60fps ticker had no
idea whether its subscribers had anything to draw. A sparkline whose samples
are all zero paints no line at all, and the toolbar had one of those on every
page, scrolling forever: including while the window was hidden to the tray.
Widgets now subscribe only while visible *and* holding a non-zero sample.

**The dashboard sampled for nobody.** Its tick ran twice a second whether or
not the page was on screen: six database aggregates, a psutil disk/network
sample, a VPN interface scan, and two rebuilt tables. Off screen it now keeps
only what the graphs need for their rolling history. `MainWindow.refresh()`
likewise returns early while the window is hidden, and catches up on show.

**Everything was built before anything was shown.** Settings is the most
expensive widget in the app (18 sections, ~100 fields, ~500ms) in a window
most sessions never open. It and the Queue page are now built on first visit.
The Dashboard stays eager because its sampler is what gives the graphs their
history.

**60fps was twice what anything here needs.** A progress bar eases over
~200ms; the graphs scroll about two pixels a frame. At 30fps the animations
take exactly as long (the easing constants divide by the frame rate) and cost
half the repaints.

## Measured and left alone

Worth recording, so nobody optimizes these twice:

- **Segment progress is already batched.** One checkpoint thread coalesces
  every worker's progress and writes it in a single transaction: 8.6 flushes
  a second for five downloads, not one write per chunk.
- **SQLite is already configured** for this workload: WAL, `synchronous`
  NORMAL, a busy timeout.
- **Rows are updated in place**, never rebuilt, while downloads run.
- **Read chunks stay a full 64KB** whether or not a speed limit is set.
- **No leaks.** Adding and removing 200 jobs returns every per-job structure
  to empty, and repeated refreshes create no extra widgets. RSS does not fall
  back on its own, which is the allocator holding freed pages, not a leak.
- **The heavy imports are lazy.** yt-dlp, libtorrent, boto3, paramiko, psutil
  and PIL all load on demand; the only third-party module imported eagerly is
  httpx.
- **The extension does not poll.** No `MutationObserver`, no interval: the
  hover button is driven by pointer events, its follow loop runs only while
  the button is visible, and the popup reads everything asynchronously so it
  paints immediately.

## The floor

For context on the download figure: five concurrent transfers through plain
`httpx`, writing to disk through the same rate limiter and doing nothing else,
cost 2.0% CPU at 1.17 MB/s on this machine. GrabLine does the same work plus a
live UI.
