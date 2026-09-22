"""写接口 ``POST /api/report`` 的测试。

重点覆盖**鉴权与校验矩阵**:每一条拒绝路径都必须有对应用例,因为在自动化链路里
一旦拒绝逻辑出错(该拒的放过、或该过的拒掉),都不会有人立刻发现。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from wxcloudrun import views
from wxcloudrun.models import Report

FIXTURE = Path(__file__).parent / "fixtures" / "daily-2026-09-22.html"

GOOD_IP = "203.0.113.7"
BAD_IP = "198.51.100.9"
API_KEY = "test-key-521016"
ENDPOINT = "/api/report"


def sample_html(body: str = "今天的内容") -> str:
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>日报</title></head><body><p>" + body + "</p></body></html>"
    )


@override_settings(
    REPORT_API_KEY=API_KEY,
    REPORT_ALLOWED_IPS=[GOOD_IP],
    REPORT_RATE_LIMIT=30,
    REPORT_PUBLIC_BASE_URL="https://example.run.tcloudbase.com",
)
class ReportApiTests(TestCase):
    def setUp(self):
        views.reset_caches()
        self.html = FIXTURE.read_text(encoding="utf-8")

    # -- 便捷方法 ---------------------------------------------------------

    def post(self, payload, *, key=API_KEY, ip=GOOD_IP, raw=None, content_type="application/json"):
        body = raw if raw is not None else json.dumps(payload, ensure_ascii=False)
        headers = {"HTTP_X_FORWARDED_FOR": ip} if ip is not None else {}
        if key is not None:
            headers["HTTP_X_API_KEY"] = key
        return self.client.post(ENDPOINT, data=body, content_type=content_type, **headers)

    def good_payload(self, **overrides):
        payload = {
            "date": "2026-09-22",
            "html": self.html,
            "title": "AI大模型行业日报 · 2026-09-22",
            "summary": "今日主旋律是…",
        }
        payload.update(overrides)
        return payload

    # -- 正常路径 ---------------------------------------------------------

    def test_首次上传返回_201_并落库(self):
        response = self.post(self.good_payload())
        self.assertEqual(response.status_code, 201)

        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["data"]["date"], "2026-09-22")
        self.assertEqual(
            body["data"]["url"], "https://example.run.tcloudbase.com/daily/2026-09-22"
        )
        self.assertTrue(body["data"]["created"])
        self.assertFalse(body["data"]["unchanged"])
        self.assertEqual(body["data"]["bytes"], len(self.html.encode("utf-8")))

        report = Report.objects.get(pk="2026-09-22")
        self.assertEqual(report.html, self.html)
        self.assertEqual(report.title, "AI大模型行业日报 · 2026-09-22")
        self.assertEqual(report.summary, "今日主旋律是…")
        self.assertEqual(report.byte_size, len(self.html.encode("utf-8")))
        self.assertEqual(report.sha256, hashlib.sha256(self.html.encode("utf-8")).hexdigest())

    def test_重复上传同样内容不写库(self):
        first = Report.objects.count()
        self.post(self.good_payload())
        response = self.post(self.good_payload())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["data"]["unchanged"])
        self.assertFalse(response.json()["data"]["created"])
        self.assertEqual(Report.objects.count(), first + 1)

    def test_重复上传变更后的内容会覆盖(self):
        self.post(self.good_payload())
        response = self.post(self.good_payload(html=sample_html("改过了"), title="新标题"))

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["data"]["created"])
        self.assertFalse(response.json()["data"]["unchanged"])

        report = Report.objects.get(pk="2026-09-22")
        self.assertEqual(report.title, "新标题")
        self.assertIn("改过了", report.html)
        self.assertEqual(Report.objects.count(), 1)

    def test_不同日期各自成条(self):
        self.post(self.good_payload())
        self.post(self.good_payload(date="2026-09-21"))
        self.assertEqual(Report.objects.count(), 2)

    def test_title_与_summary_可以省略(self):
        response = self.post({"date": "2026-09-22", "html": self.html})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Report.objects.get(pk="2026-09-22").title, "")

    def test_url_没配_base_url_时退回请求_host(self):
        with override_settings(REPORT_PUBLIC_BASE_URL=""):
            views.reset_caches()
            response = self.post(self.good_payload())
        self.assertTrue(response.json()["data"]["url"].endswith("/daily/2026-09-22"))

    # -- 配置缺失:fail-closed --------------------------------------------

    def test_未配置密钥时返回_503_而不是放行(self):
        with override_settings(REPORT_API_KEY=""):
            response = self.post(self.good_payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "api_key_not_configured")
        self.assertEqual(Report.objects.count(), 0)

    def test_未配置_IP_白名单时返回_503(self):
        with override_settings(REPORT_ALLOWED_IPS=[]):
            views.reset_caches()
            response = self.post(self.good_payload())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "allowlist_not_configured")
        self.assertEqual(Report.objects.count(), 0)

    # -- 鉴权 -------------------------------------------------------------

    def test_IP_不在白名单返回_403(self):
        response = self.post(self.good_payload(), ip=BAD_IP)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden_ip")
        self.assertEqual(Report.objects.count(), 0)

    def test_没有密钥返回_401(self):
        response = self.post(self.good_payload(), key=None)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Report.objects.count(), 0)

    def test_密钥错误返回_401(self):
        response = self.post(self.good_payload(), key="wrong")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_IP_检查排在密钥之前(self):
        """IP 不在白名单时,即使密钥也错,应报 403 而不是 401 —— 减少爆破面。"""
        response = self.post(self.good_payload(), key="wrong", ip=BAD_IP)
        self.assertEqual(response.status_code, 403)

    def test_cidr_白名单可用(self):
        with override_settings(REPORT_ALLOWED_IPS=["203.0.113.0/24"]):
            views.reset_caches()
            response = self.post(self.good_payload(), ip="203.0.113.200")
        self.assertEqual(response.status_code, 201)

    def test_xff_只看最右一段防伪造(self):
        """客户端可以往 XFF 左边塞任意值;最右一段才是最近一跳代理写入的。"""
        response = self.post(self.good_payload(), ip=f"{GOOD_IP}, {BAD_IP}")
        self.assertEqual(response.status_code, 403)

    def test_xff_最右段命中即通过(self):
        response = self.post(self.good_payload(), ip=f"{BAD_IP}, {GOOD_IP}")
        self.assertEqual(response.status_code, 201)

    # -- 请求体 -----------------------------------------------------------

    def test_方法不对返回_405(self):
        response = self.client.get(ENDPOINT, HTTP_X_FORWARDED_FOR=GOOD_IP)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST")

    def test_不是_json_返回_400(self):
        response = self.post(None, raw="这不是 json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_body")

    def test_json_不是对象返回_400(self):
        response = self.post(None, raw="[1,2,3]")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_body")

    def test_超大请求体返回_413(self):
        with override_settings(REPORT_MAX_BODY_BYTES=1024):
            response = self.post(self.good_payload(html=sample_html("x" * 4000)))
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "payload_too_large")

    def test_超限流返回_429(self):
        with override_settings(REPORT_RATE_LIMIT=2):
            views.reset_caches()
            self.assertEqual(self.post(self.good_payload(date="2026-09-01")).status_code, 201)
            self.assertEqual(self.post(self.good_payload(date="2026-09-02")).status_code, 201)
            response = self.post(self.good_payload(date="2026-09-03"))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "rate_limited")

    # -- 日期 -------------------------------------------------------------

    def test_缺少日期返回_400(self):
        response = self.post({"html": self.html})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_date")

    def test_日期格式不对返回_400(self):
        for bad in ("2026/09/22", "26-09-22", "2026-9-2", "20260922", "2026-09-22T00:00:00"):
            response = self.post(self.good_payload(date=bad))
            self.assertEqual(response.status_code, 400, bad)
            self.assertEqual(response.json()["error"]["code"], "invalid_date", bad)

    def test_不存在的日期返回_400(self):
        for bad in ("2026-13-01", "2026-02-30", "2026-00-10"):
            response = self.post(self.good_payload(date=bad))
            self.assertEqual(response.status_code, 400, bad)

    def test_日期不是字符串返回_400(self):
        response = self.post(self.good_payload(date=20260922))
        self.assertEqual(response.status_code, 400)

    # -- 内容校验 ---------------------------------------------------------

    def test_缺少_html_返回_422(self):
        response = self.post({"date": "2026-09-22"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "content_rejected")

    def test_html_不是字符串返回_422(self):
        response = self.post(self.good_payload(html={"a": 1}))
        self.assertEqual(response.status_code, 422)

    def test_含_script_返回_422(self):
        response = self.post(self.good_payload(html=sample_html("<script>alert(1)</script>")))
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "content_rejected")
        self.assertEqual(Report.objects.count(), 0)

    def test_含事件属性返回_422(self):
        response = self.post(
            self.good_payload(html="<!DOCTYPE html><html><body><p onmouseover='x()'>a</p></body></html>")
        )
        self.assertEqual(response.status_code, 422)

    def test_没有_doctype_返回_422(self):
        response = self.post(self.good_payload(html="<html><body>hi</body></html>"))
        self.assertEqual(response.status_code, 422)

    def test_错误信息里带上原因(self):
        response = self.post(self.good_payload(html=sample_html("<script>x</script>")))
        self.assertIn("script", response.json()["error"]["message"])

    # -- 响应信封 ---------------------------------------------------------

    def test_成功信封结构(self):
        body = self.post(self.good_payload()).json()
        self.assertEqual(set(body), {"ok", "data"})
        self.assertEqual(
            set(body["data"]), {"date", "url", "created", "bytes", "unchanged"}
        )

    def test_失败信封结构(self):
        body = self.post(self.good_payload(), key="wrong").json()
        self.assertEqual(set(body), {"ok", "error"})
        self.assertEqual(set(body["error"]), {"code", "message"})

    def test_中文不被转义成_ascii(self):
        response = self.post(self.good_payload(), key="wrong")
        self.assertIn("密钥", response.content.decode("utf-8"))
