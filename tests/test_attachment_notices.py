"""Synthetic attachment notices: inspect HTML descriptions without downloading files."""
# ruff: noqa: E402 -- folder Skill has no separately installed Python package.
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_public as collector

URL = "https://xuesc.lsnu.edu.cn/info/1035/1234.htm"
ATTACHMENT = "/system/_content/download.jsp?urltype=news.DownloadAttachUrl&owner=123&wbfileid=456"
TITLE = "关于合成活动安排的通知"
AT = "2026-09-20T02:00:00+00:00"


def notice(*, text="安排详见附件。", heading=TITLE, attachment=ATTACHMENT,
           filename="合成安排.xlsx", header="2026年09月19日", extra="", link_location="form"):
    link = f'<a href="{attachment}">{filename}</a>' if attachment else ""
    if link_location == "sidebar":
        link = f'<aside class="sidebar">{link}</aside>'
    if link_location == "outside":
        outside, link = link, ""
    else:
        outside = ""
    return (
        f'<html><head><title>{TITLE}-合成部门</title></head><body>'
        '<form name="_newscontent_fromname">'
        f'<h1>{heading}</h1><div>{header}</div>'
        f'<div id="vsb_content"><div class="v_news_content">{text}</div></div>'
        f'<p>附件【{link}】</p>{extra}</form>{outside}</body></html>'
    )


class AttachmentNoticeTests(unittest.TestCase):
    def parse(self, html=None, url=URL):
        # The parser must remain independent of both page and attachment network fetches.
        with patch.object(collector.urllib.request, "build_opener", side_effect=AssertionError("network prohibited")):
            return collector.parse_page(html or notice(), url)

    def test_short_structured_notice_is_an_html_observation_not_attachment_reading(self):
        page = self.parse()
        self.assertEqual(page["published_on"], "2026-09-19")
        self.assertEqual(page["attachment_count"], 1)
        self.assertEqual(page["content_scope"], "html_notice_and_attachment_links")
        self.assertIs(page["attachment_content_checked"], False)
        self.assertRegex(page["content_sha256"], r"^[a-f0-9]{64}$")

    def test_attachment_url_and_name_each_change_observation_fingerprint(self):
        original = self.parse()["content_sha256"]
        replaced = self.parse(notice(attachment=ATTACHMENT.replace("456", "789")))
        renamed = self.parse(notice(filename="合成安排修订.xlsx"))
        self.assertNotEqual(original, replaced["content_sha256"])
        self.assertNotEqual(original, renamed["content_sha256"])
        reordered = self.parse(notice(attachment="/system/_content/download.jsp?wbfileid=456&owner=123&urltype=news.DownloadAttachUrl"))
        self.assertEqual(original, reordered["content_sha256"])

    def test_short_page_requires_matching_h1_and_document_title(self):
        for heading in ("", "其他栏目"):
            with self.subTest(heading=heading), self.assertRaises(collector.ShortNoticeWithoutVerifiedAttachment):
                self.parse(notice(heading=heading))

    def test_short_notice_requires_named_form_and_nonempty_article_container(self):
        examples = [
            notice().replace('_newscontent_fromname', 'sidebar_form'),
            notice().replace('class="v_news_content"', 'class="navigation"'),
            notice(text=""),
            notice().replace('</form>', ''),
        ]
        for html in examples:
            with self.subTest(html=html), self.assertRaises(ValueError):
                self.parse(html)

    def test_offsite_unrelated_and_malformed_download_links_cannot_admit_short_notice(self):
        invalid = [
            "https://outside.example" + ATTACHMENT,
            "https://jiaowc.lsnu.edu.cn" + ATTACHMENT,
            "/sidebar/file.xlsx",
            ATTACHMENT + "&wbfileid=999",
            ATTACHMENT.replace("owner=123", "owner=abc"),
            ATTACHMENT.replace("news.DownloadAttachUrl", "other"),
            ATTACHMENT + "&redirect=https://outside.example",
            "",
        ]
        for attachment in invalid:
            with self.subTest(attachment=attachment), self.assertRaises(collector.ShortNoticeWithoutVerifiedAttachment):
                self.parse(notice(attachment=attachment))

    def test_outside_form_and_sidebar_attachments_do_not_qualify(self):
        for location in ("outside", "sidebar"):
            with self.subTest(location=location), self.assertRaises(collector.ShortNoticeWithoutVerifiedAttachment):
                self.parse(notice(link_location=location))

    def test_challenge_is_rejected_even_with_notice_structure_and_attachment(self):
        for message in ("请输入验证码下载附件", "人机验证", "Access Denied", "access denied"):
            with self.subTest(message=message), self.assertRaises(collector.OfficialPageChallenge):
                self.parse(notice(extra=f"<div>{message}</div>"))

    def test_short_navigation_and_non_notice_urls_are_rejected(self):
        with self.assertRaises(collector.ShortNoticeWithoutVerifiedAttachment):
            self.parse('<title>导航</title><nav>首页栏目</nav>')
        with self.assertRaises(collector.ShortNoticeWithoutVerifiedAttachment):
            self.parse(notice(), url="https://xuesc.lsnu.edu.cn/")

    def test_dates_inside_article_or_attachment_name_are_not_publication_dates(self):
        page = self.parse(notice(header="", text="活动日期2025年01月02日", filename="2024年03月04日合成安排.xlsx"))
        self.assertIsNone(page["published_on"])

    def test_attachment_description_counts_are_deduplicated_and_script_text_is_ignored(self):
        page = self.parse(notice(extra=f'<a href="{ATTACHMENT}">合成安排.xlsx</a><script>Access Denied</script>'))
        self.assertEqual(page["attachment_count"], 1)

    def test_normal_long_html_keeps_original_content_hash_and_existing_validation(self):
        text = "这是合成的完整通知正文。" * 5
        html = f'<title>合成通知</title><div class="v_news_content">{text}</div>'
        page = self.parse(html)
        parsed = collector.PageText()
        parsed.feed(html)
        self.assertEqual(page["content_sha256"], parsed.result()["content_sha256"])
        self.assertEqual(page["attachment_count"], 0)
        self.assertEqual(page["content_scope"], "html_page")

    def test_observation_scope_is_saved_in_snapshot_and_errors_do_not_leak_html(self):
        page = self.parse()
        cards = [{"source_url": URL, "title": TITLE, "id": "synthetic"}]
        value = collector.collect(cards, [], fetch=lambda _: page, collected_at=AT)
        row = value["items"][0]
        self.assertEqual(row["content_scope"], "html_notice_and_attachment_links")
        self.assertEqual(row["attachment_count"], 1)
        self.assertIs(row["attachment_content_checked"], False)
        error = collector.ShortNoticeWithoutVerifiedAttachment("synthetic private-looking arbitrary response")
        self.assertEqual(collector.error_code(error), "ShortNoticeWithoutVerifiedAttachment")
        self.assertFalse(collector.transient_error(error))


if __name__ == "__main__":
    unittest.main()
