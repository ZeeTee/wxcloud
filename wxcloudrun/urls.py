"""URL 路由。

刻意用 ``re_path`` 而不是 ``path``:模板原来用的是 ``django.conf.urls.url``,
它在 Django 4.0 已被移除;``re_path`` 是它的直接替代。同时接受带不带结尾斜杠两种写法,
避免 POST 被 ``APPEND_SLASH`` 302 重定向(重定向会丢掉请求体)。
"""

from django.urls import re_path

from . import views

urlpatterns = [
    # 写入:唯一写接口
    re_path(r"^api/report/?$", views.report_api, name="report-api"),
    # 读取:原样返回入库的 HTML
    re_path(r"^daily/(?P<date>\d{4}-\d{2}-\d{2})/?$", views.daily, name="daily"),
    # 存活探针
    re_path(r"^healthz/?$", views.healthz, name="healthz"),
    # 服务说明
    re_path(r"^$", views.home, name="home"),
]

# DEBUG=False 时由它接管 404(API 路径返回 JSON 信封,其余返回页面)
handler404 = "wxcloudrun.views.page_not_found"
