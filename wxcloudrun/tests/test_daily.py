"""读路径、探针与错误页的测试。

这里有一条是**整个方案的验收核心**:详情页返回的字节必须与入库的 HTML 完全一致。
它把"渲染问题"和"服务问题"分开 —— 网页上显示不对,就一定是渲染器的问题。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from django.test import TestCase, override_settings
from django.urls import reverse

from wxcloudrun.models import MediumTextField, Report

FIXTURE = Path(__file__).parent / "fixtures" / "daily-2026-09-22.html"
DAILY = "/daily/2026-09-22"


class DailyViewTests(TestCase):
    def setUp(self):
        self.html = FIXTURE.read_text(encoding="utf-8")
        self.report = Report.objects.create(
            report_date="2026-09-22",
            title="AI大模型行业日报 · 2026-09-22",
            summary="今日主旋律是…",
            html=self.html,
            byte_size=len(self.html.encode("utf-8")),
            sha256=hashlib.sha256(self.html.encode("utf-8")).hexdigest(),
        )

    def test_返回的字节与入库内容完全一致(self):
        """零加工的验收断言:不许有任何改写、转义或重排版。"""
        response = self.client.get(DAILY)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.html.encode("utf-8"))

    def test_内容类型是_html_且声明_utf8(self):
        response = self.client.get(DAILY)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")

    def test_response_不是下载(self):
        response = self.client.get(DAILY)
        self.assertNotIn("Content-Disposition", response)

    def test_中文不乱码(self):
        Report.objects.filter(pk="2026-09-22").update(
            html="<!DOCTYPE html><html><body><p>中文标题测试</p></body></html>"
        )
        response = self.client.get(DAILY)
        self.assertIn("中文标题测试", response.content.decode("utf-8"))

    def test_安全响应头齐全(self):
        response = self.client.get(DAILY)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        csp = response["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("'unsafe-eval'", csp)

    def test_etag_与_304(self):
        first = self.client.get(DAILY)
        etag = first["ETag"]
        self.assertEqual(etag, f'"{self.report.sha256}"')

        second = self.client.get(DAILY, HTTP_IF_NONE_MATCH=etag)
        self.assertEqual(second.status_code, 304)
        self.assertEqual(second.content, b"")

    def test_日期不存在返回_404_页面(self):
        response = self.client.get("/daily/2026-01-01")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("没有找到这一天的日报", response.content.decode("utf-8"))

    def test_404_页面显示请求的日期(self):
        response = self.client.get("/daily/2026-01-01")
        self.assertIn("2026-01-01", response.content.decode("utf-8"))

    def test_路径不合法时也让全局_404_接管(self):
        for bad in ("/daily/abc", "/daily/2026-9-2", "/daily/2026-09-22/extra"):
            response = self.client.get(bad)
            self.assertEqual(response.status_code, 404, bad)

    def test_带结尾斜杠也能访问(self):
        self.assertEqual(self.client.get(DAILY + "/").status_code, 200)

    def test_不接受_POST(self):
        response = self.client.post(DAILY)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "GET, HEAD")

    def test_HEAD_请求可用(self):
        response = self.client.head(DAILY)
        self.assertEqual(response.status_code, 200)

    def test_入库内容不会被当模板渲染(self):
        """存的是 HTML,不是模板语法;若走模板引擎,{{ }} 会被吃掉。"""
        Report.objects.filter(pk="2026-09-22").update(
            html="<!DOCTYPE html><html><body><p>{{ 1+1 }}</p></body></html>"
        )
        response = self.client.get(DAILY)
        self.assertIn(b"{{ 1+1 }}", response.content)

    def test_反查_url(self):
        self.assertEqual(reverse("daily", kwargs={"date": "2026-09-22"}), DAILY)


class HealthzAndHomeTests(TestCase):
    def test_healthz_返回_ok(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"ok")

    def test_healthz_不依赖数据库(self):
        """存活探针不该因为 DB 故障而失败(否则平台会不停重启容器)。"""
        with self.assertNumQueries(0):
            self.client.get("/healthz")

    def test_首页返回说明页(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("AI 大模型行业日报", response.content.decode("utf-8"))

    def test_首页不含数据库内容(self):
        with self.assertNumQueries(0):
            self.client.get("/")

    def test_未知接口返回_json_信封(self):
        response = self.client.get("/api/nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_未知页面返回_html_404(self):
        response = self.client.get("/nope")
        self.assertEqual(response.status_code, 404)
        self.assertIn("text/html", response["Content-Type"])


class ModelTests(TestCase):
    def test_mysql_下_html_列是_mediumtext(self):
        field = Report._meta.get_field("html")
        self.assertIsInstance(field, MediumTextField)

        class FakeConnection:
            vendor = "mysql"

        class FakeSqliteConnection:
            vendor = "sqlite"

        self.assertEqual(field.db_type(FakeConnection()), "mediumtext")
        self.assertEqual(field.db_type(FakeSqliteConnection()), "text")

    def test_主键是日期(self):
        self.assertEqual(Report._meta.pk.name, "report_date")

    def test_表名(self):
        self.assertEqual(Report._meta.db_table, "reports")

    def test_同一天只可能有一条(self):
        Report.objects.create(report_date="2026-09-22", html="a")
        with self.assertRaises(Exception):
            Report.objects.create(report_date="2026-09-22", html="b")
