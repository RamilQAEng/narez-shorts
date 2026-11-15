"""Links from temporary file hosts that agents use to hand over a video."""
import file_hosts


def test_tmpfiles_link_is_refreshed_through_the_page():
    stale = "https://tmpfiles.org/dl/1787841143.abc/wAwNGnPW0lXW/video.mp4"
    fresh = "https://tmpfiles.org/dl/1787841582.def/wAwNGnPW0lXW/video.mp4"
    seen = {}
    def fetch(url):
        seen["page"] = url
        return f'<a href="{fresh}">Download</a>'
    assert file_hosts.resolve_tmpfiles(stale, fetch=fetch) == fresh
    assert seen["page"] == "https://tmpfiles.org/wAwNGnPW0lXW/video.mp4"
    assert file_hosts.resolve_tmpfiles("https://tmpfiles.org/wAwNGnPW0lXW/video.mp4", fetch=fetch) == fresh
    assert file_hosts.resolve_tmpfiles(stale, fetch=lambda u: "<html>no link</html>") == stale
    assert not file_hosts.is_tmpfiles("https://youtube.com/watch?v=x")
