from app import native

AD_ENDS = 1_900_000_000


class YoutubeDL:
    def __init__(self, options):
        self.cookiejar = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        # As yt-dlp merges bestvideo+bestaudio: the ad wait is on each format only.
        video = {"url": "https://media.example/v.mp4", "ext": "mp4", "vcodec": "avc1", "acodec": "none",
                 "height": 1080, "available_at": AD_ENDS}
        audio = {"url": "https://media.example/a.m4a", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2",
                 "available_at": AD_ENDS}
        return {"title": "t", "duration": 60, "formats": [video, audio], "requested_formats": [video, audio]}


class Cookies:
    def copy_into(self, jar):
        pass

    def merge_from(self, jar):
        pass


def test_the_selected_formats_ad_wait_is_reported(monkeypatch):
    monkeypatch.setattr(native, "YoutubeDL", YoutubeDL)
    assert native._extract("https://www.youtube.com/watch?v=ad", Cookies())["available_at"] == AD_ENDS
