"""security.py 的单元测试。

这一层是服务的全部防线,所以测试写得比业务代码还细 —— 尤其是**误杀**方向:
内容校验把正常日报拒掉,会让整条自动发布链路静默失败。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from django.test import SimpleTestCase, RequestFactory

from wxcloudrun import security

FIXTURE = Path(__file__).parent / "fixtures" / "daily-2026-09-22.html"


class UrlSchemeTests(SimpleTestCase):
    def test_绝对地址(self):
        self.assertEqual(security.url_scheme("https://www.ithome.com/1/005/496.htm"), "https")
        self.assertEqual(security.url_scheme("HTTP://example.com"), "http")

    def test_相对路径与锚点是空协议(self):
        self.assertEqual(security.url_scheme("/daily/2026-09-22"), "")
        self.assertEqual(security.url_scheme("#top"), "")
        self.assertEqual(security.url_scheme(""), "")

    def test_大小写混写(self):
        self.assertEqual(security.url_scheme("JavaScript:alert(1)"), "javascript")

    def test_协议里夹控制字符仍能识别(self):
        # 浏览器解析协议时会忽略这些字符,所以不能被它们绕过
        self.assertEqual(security.url_scheme("java\tscript:alert(1)"), "javascript")
        self.assertEqual(security.url_scheme("java\nscript:alert(1)"), "javascript")
        self.assertEqual(security.url_scheme(" javascript:x"), "javascript")

    def test_data_协议被识别出来(self):
        self.assertEqual(security.url_scheme("data:text/html,<h1>x"), "data")

    def test_mailto_正常(self):
        self.assertEqual(security.url_scheme("mailto:a@b.com"), "mailto")


class NormalizeIpTests(SimpleTestCase):
    def test_普通_ipv4(self):
        self.assertEqual(security.normalize_ip("1.2.3.4"), "1.2.3.4")
        self.assertEqual(security.normalize_ip("  1.2.3.4  "), "1.2.3.4")

    def test_带端口的_ipv4(self):
        self.assertEqual(security.normalize_ip("1.2.3.4:8080"), "1.2.3.4")

    def test_ipv6(self):
        self.assertEqual(security.normalize_ip("::1"), "::1")
        self.assertEqual(security.normalize_ip("[2001:db8::1]:443"), "2001:db8::1")

    def test_非法输入返回空串(self):
        for bad in ("", "   ", "unknown", "not-an-ip", "1.2.3", "999.1.1.1"):
            self.assertEqual(security.normalize_ip(bad), "", bad)


class ParseNetworksTests(SimpleTestCase):
    def test_单_ip_与_cidr(self):
        networks, invalid = security.parse_networks(["203.0.113.7", "10.0.0.0/8"])
        self.assertEqual(invalid, [])
        self.assertEqual(len(networks), 2)

    def test_非法项被单独返回(self):
        networks, invalid = security.parse_networks(["203.0.113.7", "不是IP", ""])
        self.assertEqual(len(networks), 1)
        self.assertEqual(invalid, ["不是IP"])

    def test_空输入(self):
        self.assertEqual(security.parse_networks([]), ([], []))
        self.assertEqual(security.parse_networks(None), ([], []))


class IpAllowedTests(SimpleTestCase):
    def setUp(self):
        self.nets, _ = security.parse_networks(["203.0.113.7", "10.0.0.0/8"])

    def test_精确命中(self):
        self.assertTrue(security.ip_allowed(["203.0.113.7"], self.nets))

    def test_命中_任意一个候选即可(self):
        self.assertTrue(security.ip_allowed(["198.51.100.1", "203.0.113.7"], self.nets))

    def test_cidr_命中(self):
        self.assertTrue(security.ip_allowed(["10.1.2.3"], self.nets))

    def test_不命中(self):
        self.assertFalse(security.ip_allowed(["198.51.100.1"], self.nets))

    def test_空白名单一律拒绝(self):
        self.assertFalse(security.ip_allowed(["203.0.113.7"], []))

    def test_ipv6_不会匹配_ipv4_网段(self):
        nets, _ = security.parse_networks(["203.0.113.7"])
        self.assertFalse(security.ip_allowed(["::ffff:203.0.113.7"], nets))


class KeyMatchesTests(SimpleTestCase):
    def test_相同(self):
        self.assertTrue(security.key_matches("521016", "521016"))

    def test_不同(self):
        self.assertFalse(security.key_matches("521017", "521016"))

    def test_空值一律不通过(self):
        self.assertFalse(security.key_matches("", "521016"))
        self.assertFalse(security.key_matches("521016", ""))
        self.assertFalse(security.key_matches("", ""))


class AuditHtmlTests(SimpleTestCase):
    """内容审计:既要拒绝危险内容,也**不能误杀**真实产物。"""

    def test_真实日报产物必须通过(self):
        """最重要的一条:渲染器产出的真实 HTML 不能被拒。"""
        html = FIXTURE.read_text(encoding="utf-8")
        self.assertEqual(security.audit_html(html), [])

    def test_必须_doctype_开头(self):
        problems = security.audit_html("<html><body>hi</body></html>")
        self.assertTrue(any("DOCTYPE" in p for p in problems), problems)

    def test_允许正常的结构(self):
        self.assertEqual(
            security.audit_html(
                "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                "<style>body{color:#333}</style></head>"
                "<body><section><p style='line-height:1.8'>正文</p>"
                "<a href='https://example.com/a?b=1'>链接</a></section></body></html>"
            ),
            [],
        )

    def test_拒绝_script_标签(self):
        problems = security.audit_html("<!DOCTYPE html><html><body><script>x</script></body></html>")
        self.assertTrue(problems)

    def test_拒绝_script_大小写混写(self):
        problems = security.audit_html("<!DOCTYPE html><html><body><ScRiPt>x</ScRiPt></body></html>")
        self.assertTrue(problems)

    def test_拒绝_iframe(self):
        problems = security.audit_html("<!DOCTYPE html><html><body><iframe src='https://x'></iframe></body></html>")
        self.assertTrue(problems)

    def test_拒绝事件属性(self):
        problems = security.audit_html("<!DOCTYPE html><html><body><p onclick='x()'>a</p></body></html>")
        self.assertTrue(any("onclick" in p for p in problems), problems)

    def test_拒绝_javascript_协议(self):
        problems = security.audit_html("<!DOCTYPE html><html><body><a href='javascript:alert(1)'>x</a></body></html>")
        self.assertTrue(any("协议" in p for p in problems), problems)

    def test_拒绝协议里夹制表符的绕过写法(self):
        problems = security.audit_html(
            "<!DOCTYPE html><html><body><a href='java\tscript:alert(1)'>x</a></body></html>"
        )
        self.assertTrue(any("协议" in p for p in problems), problems)

    def test_拒绝_data_协议链接(self):
        problems = security.audit_html("<!DOCTYPE html><html><body><a href='data:text/html,x'>x</a></body></html>")
        self.assertTrue(any("协议" in p for p in problems), problems)

    def test_拒绝_svg(self):
        problems = security.audit_html("<!DOCTYPE html><html><body><svg/onload=alert(1)></body></html>")
        self.assertTrue(problems)

    def test_拒绝_meta_refresh(self):
        problems = security.audit_html(
            "<!DOCTYPE html><html><head><meta http-equiv='refresh' content='0;url=https://evil'></head></html>"
        )
        self.assertTrue(any("refresh" in p for p in problems), problems)

    def test_拒绝_form(self):
        problems = security.audit_html("<!DOCTYPE html><html><body><form action='https://evil'>x</form></body></html>")
        self.assertTrue(problems)

    def test_不误杀正文里的_only_字样(self):
        """裸正则查 on\\w+= 会把这样的正常文本判成事件属性 —— 必须不命中。"""
        html = (
            "<!DOCTYPE html><html><body>"
            "<p>This offer is only = valid today, and one = two.</p>"
            "<p>monospace = font</p>"
            "</body></html>"
        )
        self.assertEqual(security.audit_html(html), [])

    def test_不误杀正文里的尖括号转义文本(self):
        html = "<!DOCTYPE html><html><body><p>&lt;script&gt; 是标签</p></body></html>"
        self.assertEqual(security.audit_html(html), [])

    def test_空内容(self):
        self.assertEqual(security.audit_html(""), ["内容为空"])

    def test_同一问题只报一次(self):
        problems = security.audit_html(
            "<!DOCTYPE html><html><body><script>a</script><script>b</script></body></html>"
        )
        self.assertEqual(len(problems), len(set(problems)))


class CandidateIpsTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, **meta):
        return self.factory.post("/api/report", **meta)

    def test_取_xff_最右一段(self):
        """XFF 左边的段由客户端提供、可伪造,只有最右一段是最近代理写入的。"""
        request = self._request(
            HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.7",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(security.candidate_ips(request), ["203.0.113.7", "10.0.0.1"])

    def test_多个头都会看(self):
        request = self._request(
            HTTP_X_ORIGINAL_FORWARDED_FOR="203.0.113.7",
            HTTP_X_REAL_IP="198.51.100.9",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(
            security.candidate_ips(request), ["203.0.113.7", "198.51.100.9", "10.0.0.1"]
        )

    def test_去重(self):
        request = self._request(HTTP_X_REAL_IP="10.0.0.1", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(security.candidate_ips(request), ["10.0.0.1"])

    def test_没有转发头时退回_remote_addr(self):
        request = self._request(REMOTE_ADDR="10.0.0.1")
        self.assertEqual(security.candidate_ips(request), ["10.0.0.1"])

    def test_非法值被丢弃(self):
        request = self._request(HTTP_X_FORWARDED_FOR="unknown", REMOTE_ADDR="10.0.0.1")
        self.assertEqual(security.candidate_ips(request), ["10.0.0.1"])

    def test_可以指定只信某个头(self):
        request = self._request(
            HTTP_X_FORWARDED_FOR="1.2.3.4",
            HTTP_X_ORIGINAL_FORWARDED_FOR="203.0.113.7",
            REMOTE_ADDR="10.0.0.1",
        )
        self.assertEqual(
            security.candidate_ips(request, ("x-original-forwarded-for",))[0], "203.0.113.7"
        )

    def test_observed_headers_只含存在的头(self):
        request = self._request(HTTP_X_FORWARDED_FOR="203.0.113.7", REMOTE_ADDR="10.0.0.1")
        snapshot = security.observed_headers(request)
        self.assertEqual(snapshot.get("x-forwarded-for"), "203.0.113.7")
        self.assertEqual(snapshot.get("remote_addr"), "10.0.0.1")
        self.assertNotIn("x-real-ip", snapshot)


class RateLimiterTests(unittest.TestCase):
    def test_超过上限即拒绝(self):
        limiter = security.RateLimiter(limit=3, window_seconds=60)
        self.assertTrue(limiter.allow("a", now=0))
        self.assertTrue(limiter.allow("a", now=1))
        self.assertTrue(limiter.allow("a", now=2))
        self.assertFalse(limiter.allow("a", now=3))

    def test_不同_key_互不影响(self):
        limiter = security.RateLimiter(limit=1, window_seconds=60)
        self.assertTrue(limiter.allow("a", now=0))
        self.assertFalse(limiter.allow("a", now=0))
        self.assertTrue(limiter.allow("b", now=0))

    def test_窗口滑出后恢复(self):
        limiter = security.RateLimiter(limit=1, window_seconds=10)
        self.assertTrue(limiter.allow("a", now=0))
        self.assertFalse(limiter.allow("a", now=5))
        self.assertTrue(limiter.allow("a", now=11))

    def test_大量不同_key_不会无限增长(self):
        limiter = security.RateLimiter(limit=1, window_seconds=1, max_keys=50)
        for i in range(500):
            limiter.allow(f"key-{i}", now=float(i))
        self.assertLessEqual(len(limiter._hits), 100)
