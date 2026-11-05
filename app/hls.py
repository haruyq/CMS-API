"""Just enough HLS (RFC 8216) to pick a variant and to pass a live media playlist on with some segments left out."""

import datetime
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin

# Playlist-level tags; every other tag belongs to the segment that follows it.
_HEADER_TAGS = ("#EXTM3U", "#EXT-X-VERSION", "#EXT-X-TARGETDURATION", "#EXT-X-MEDIA-SEQUENCE",
                "#EXT-X-DISCONTINUITY-SEQUENCE", "#EXT-X-INDEPENDENT-SEGMENTS", "#EXT-X-PLAYLIST-TYPE",
                "#EXT-X-START", "#EXT-X-ALLOW-CACHE", "#EXT-X-ENDLIST")
_URI_ATTRIBUTE = re.compile(r'URI="([^"]*)"')


@dataclass(frozen=True)
class Segment:
    sequence: int
    uri: str
    duration: float
    # Wall-clock time (Unix seconds) of the segment's first frame, from EXT-X-PROGRAM-DATE-TIME; None if absent.
    starts_at: float | None
    # Its own tag lines, and the EXT-X-KEY in effect for it (repeated when earlier segments are left out).
    tags: tuple[str, ...]
    key: str | None
    discontinuity: bool


@dataclass(frozen=True)
class MediaPlaylist:
    header: tuple[str, ...]
    segments: tuple[Segment, ...]
    discontinuity_sequence: int
    ended: bool

    def render(self, keep: Callable[[Segment], bool], uri: Callable[[Segment], str] = lambda s: s.uri) -> str:
        """The playlist with only the segments `keep` accepts, each pointing at `uri(segment)`."""
        kept = [s for s in self.segments if keep(s)]
        dropped_discontinuities = sum(
            s.discontinuity for s in self.segments if kept and s.sequence < kept[0].sequence)
        lines = [*self.header,
                 f"#EXT-X-MEDIA-SEQUENCE:{kept[0].sequence if kept else 0}",
                 f"#EXT-X-DISCONTINUITY-SEQUENCE:{self.discontinuity_sequence + dropped_discontinuities}"]
        for i, segment in enumerate(kept):
            has_key = any(tag.startswith("#EXT-X-KEY") for tag in segment.tags)
            if i == 0 and segment.key and not has_key:
                lines.append(segment.key)
            lines += [*segment.tags, uri(segment)]
        if self.ended:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Variant:
    uri: str
    stream_inf: str
    # The EXT-X-MEDIA renditions it uses (its separate audio, if any), URIs made absolute.
    renditions: tuple[str, ...]


def best_variant(master: str, base_url: str, lowest: bool = False) -> Variant | None:
    """The highest-bandwidth variant of a master playlist (`lowest`: the lowest); None if `master` is a media
    playlist."""
    lines = [line.strip() for line in master.splitlines()]
    media = [_URI_ATTRIBUTE.sub(lambda m: f'URI="{urljoin(base_url, m.group(1))}"', line)
             for line in lines if line.startswith("#EXT-X-MEDIA:")]
    best, best_bandwidth = None, None
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            bandwidth = int(m.group(1)) if (m := re.search(r"(?:^|[:,])BANDWIDTH=(\d+)", line)) else 0
            uri = next((l for l in lines[i + 1:] if l and not l.startswith("#")), None)
            if uri and (best_bandwidth is None or (bandwidth < best_bandwidth if lowest else bandwidth > best_bandwidth)):
                groups = set(re.findall(r'(?:AUDIO|SUBTITLES)="([^"]*)"', line))
                used = tuple(m for m in media if re.search(r'GROUP-ID="([^"]*)"', m).group(1) in groups)
                best, best_bandwidth = Variant(urljoin(base_url, uri), line, used), bandwidth
    return best


def only(variant: Variant) -> str:
    """A master playlist offering just `variant`, so a player can't settle for a lower quality."""
    return "\n".join(["#EXTM3U", "#EXT-X-INDEPENDENT-SEGMENTS", *variant.renditions, variant.stream_inf,
                      variant.uri]) + "\n"


def parse_media(text: str, base_url: str) -> MediaPlaylist:
    header: list[str] = []
    segments: list[Segment] = []
    pending: list[str] = []
    sequence = discontinuity_sequence = 0
    key = None
    duration = 0.0
    starts_at = None
    discontinuity = ended = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            discontinuity_sequence = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith(_HEADER_TAGS):
            header.append(line)
        elif line.startswith("#"):
            line = _URI_ATTRIBUTE.sub(lambda m: f'URI="{urljoin(base_url, m.group(1))}"', line)
            if line.startswith("#EXTINF:"):
                duration = float(line[8:].split(",", 1)[0])
            elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                starts_at = datetime.datetime.fromisoformat(line.split(":", 1)[1].replace("Z", "+00:00")).timestamp()
            elif line.startswith("#EXT-X-KEY"):
                key = line
            elif line == "#EXT-X-DISCONTINUITY":
                discontinuity = True
            pending.append(line)
        else:
            if starts_at is None and segments and segments[-1].starts_at is not None:
                starts_at = segments[-1].starts_at + segments[-1].duration
            segments.append(Segment(sequence, urljoin(base_url, line), duration, starts_at, tuple(pending), key,
                                    discontinuity))
            sequence += 1
            pending, starts_at, discontinuity = [], None, False
    return MediaPlaylist(tuple(header), tuple(segments), discontinuity_sequence, ended)
