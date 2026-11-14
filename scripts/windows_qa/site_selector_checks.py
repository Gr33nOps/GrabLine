"""Site selector contracts on real browser DOM, explicitly not live-site passes."""

from __future__ import annotations

import json
from pathlib import Path

from command import command
from common import case

source = Path(__file__).resolve().parents[2] / "extension/content/sites"
fixtures = [
    (
        "YOUTUBE-WATCH",
        "youtube",
        "https://www.youtube.com/watch?v=qa123",
        '<div id="movie_player"><video id="qa-target"></video></div>',
        "https://www.youtube.com/watch?v=qa123",
    ),
    (
        "YOUTUBE-SHORTS",
        "youtube",
        "https://www.youtube.com/",
        '<a class="reel-item-endpoint" href="/shorts/qa123"><img id="qa-target"></a>',
        "https://www.youtube.com/shorts/qa123",
    ),
    (
        "YOUTUBE-MUSIC",
        "youtube",
        "https://music.youtube.com/watch?v=qa123",
        '<ytmusic-player-bar id="qa-target"></ytmusic-player-bar>',
        "https://music.youtube.com/watch?v=qa123",
    ),
    (
        "VIMEO",
        "vimeo",
        "https://vimeo.com/",
        '<a href="/12345678?from=fixture"><img id="qa-target"></a>',
        "https://vimeo.com/12345678",
    ),
    (
        "X",
        "x",
        "https://x.com/home",
        '<article><a href="/qa/status/1234567/photo/1">Time</a>'
        '<div data-testid="videoPlayer"><video id="qa-target"></video></div></article>',
        "https://x.com/qa/status/1234567",
    ),
    (
        "SOUNDCLOUD",
        "soundcloud",
        "https://soundcloud.com/discover",
        '<div class="playbackSoundBadge"><a class="playbackSoundBadge__titleLink" '
        'href="/qa-artist/qa-song"></a><span id="qa-target"></span></div>',
        "https://soundcloud.com/qa-artist/qa-song",
    ),
    (
        "SOUNDCLOUD-REJECT-DISCOVER",
        "soundcloud",
        "https://soundcloud.com/discover",
        '<a class="soundTitle__title" id="qa-target" href="/discover/sets">Browse</a>',
        None,
    ),
    (
        "REDDIT",
        "reddit",
        "https://www.reddit.com/",
        '<shreddit-post post-type="video" permalink="/r/qa/comments/123/fixture/?share=qa">'
        '<shreddit-player><video id="qa-target"></video></shreddit-player></shreddit-post>',
        "https://www.reddit.com/r/qa/comments/123/fixture/",
    ),
    (
        "INSTAGRAM",
        "instagram",
        "https://www.instagram.com/",
        '<a href="/reels/QA123/?utm=fixture"><img id="qa-target"></a>',
        "https://www.instagram.com/reel/QA123/",
    ),
    (
        "DAILYMOTION-WATCH",
        "dailymotion",
        "https://www.dailymotion.com/video/x123abc",
        '<div id="player-wrapper"><video id="qa-target"></video></div>',
        "https://www.dailymotion.com/video/x123abc",
    ),
    (
        "DAILYMOTION-GEO",
        "dailymotion",
        "https://geo.dailymotion.com/player.html?video=x123abc",
        '<video id="qa-target"></video>',
        "https://www.dailymotion.com/video/x123abc",
    ),
]
for name, module, location, html, expected in fixtures:
    with case(
        "SITE-DOM-" + name,
        "controlled real browser DOM selector contract; not live-site integration",
    ) as evidence:
        script = (source / (module + ".js")).read_text(encoding="utf-8")
        js = """({script,location,html})=>{
            const host=document.createElement('section');host.innerHTML=html;
            document.body.append(host);
            let resolve;
            const closestDeep=(target,selector)=>{
                for(let e=target;e; e=e.parentElement||e.getRootNode()?.host){
                    if(e.matches?.(selector))return e;
                }return null;
            };
            try{
                new Function('globalThis','location',script)({
                    grablineSiteButton:c=>{resolve=c.resolve},
                    grablineButtonKit:{closestDeep}
                },new URL(location));
                const answer=resolve(host.querySelector('#qa-target'));
                return answer?{url:answer.url,anchorConnected:answer.anchor.isConnected}:null;
            }finally{host.remove();}
        }"""
        result = command(
            "evaluate",
            js="("
            + js
            + ")("
            + json.dumps({"script": script, "location": location, "html": html})
            + ")",
        )
        if expected is None:
            assert result is None, result
        else:
            assert result and result["url"] == expected and result["anchorConnected"], result
        evidence.update(
            location_fixture=location,
            result=result,
            expected_url=expected,
            limitation=(
                "Location and host DOM are controlled fixtures; "
                "no live-site or native-download claim"
            ),
        )
