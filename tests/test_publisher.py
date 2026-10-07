from services.publisher import CAPTION_LIMIT, build_caption, notes_to_text, release_title, visible_length

URL = "https://github.com/dmytrokurochkin/MZGram-Android/releases/tag/v1.0.0"


def test_short_notes_are_kept_whole():
    caption = build_caption("MZGram Android 1.0.0", "• One\n• Two", URL)
    assert "• One\n• Two" in caption
    assert "…" not in caption


def test_long_notes_are_cut_at_a_line_with_the_link_kept():
    notes = "\n".join(f"• Change number {i} with some words" for i in range(200))
    caption = build_caption("MZGram Android 1.0.0", notes, URL)
    assert visible_length(caption) <= CAPTION_LIMIT
    assert caption.rstrip().endswith("</a>")
    assert URL in caption
    assert "…" in caption
    assert "• Change number 0 with some words" in caption


def test_one_huge_line_is_cut_by_characters():
    caption = build_caption("Title", "x" * 5000, URL)
    assert visible_length(caption) <= CAPTION_LIMIT
    assert "xxx" in caption


def test_html_in_notes_is_escaped_and_counted_as_text():
    notes = "\n".join("<b>&</b> " * 20 for _ in range(30))
    caption = build_caption("A <b> title", notes, URL)
    assert visible_length(caption) <= CAPTION_LIMIT
    assert "<b>A &lt;b&gt; title</b>" in caption
    assert "&amp;" in caption


def test_utf16_length_is_used_for_emoji():
    notes = "\n".join("🎉" * 30 for _ in range(40))
    caption = build_caption("Title", notes, URL)
    assert visible_length(caption) <= CAPTION_LIMIT


def test_markdown_becomes_plain_text():
    text = notes_to_text("## What's new\r\n\r\n\r\n\r\n- **Bold** item\n* [link](https://x)\n`code`")
    assert text == "What's new\n\n• Bold item\n• link\ncode"


def test_release_title():
    assert release_title("o/MZGram-Android", {"tag_name": "v1.0.0", "name": ""}) == "MZGram Android v1.0.0"
    assert release_title("o/MZGram-Android", {"tag_name": "v1.1.0-beta.1", "name": "X", "prerelease": True}) == "X (beta)"
