"""响应安全头。

Django 的 ``SecurityMiddleware`` 覆盖 nosniff / Referrer-Policy / X-Frame-Options,
但**不提供 CSP**。本站会把上传来的 HTML 原样返回给浏览器,所以 CSP 是最有价值的一道
兜底:即使内容校验被绕过,``default-src 'none'`` 也能让注入的脚本无法加载任何东西。

这里显式把四个头都写上,而不是依赖 settings 里的开关 —— 让"返回的响应到底带了什么头"
在中间件一处可见,也便于单测。
"""

from __future__ import annotations

#: 日报 HTML 只用到内联样式(``<style>`` 块与 style 属性),没有任何脚本与外部资源,
#: 所以可以收到最紧。``img-src data:`` 是给将来可能内嵌的图片留的口子。
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """给所有响应加安全头。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # 用 []= 覆盖而不是 setdefault:值由本文件唯一定义,避免被上游悄悄改掉。
        response["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "no-referrer"
        response["X-Frame-Options"] = "DENY"
        return response
