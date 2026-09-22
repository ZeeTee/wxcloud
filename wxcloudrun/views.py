"""视图:日报归档服务的全部端点。

     POST /api/report            写入(唯一写接口,密钥 + IP 白名单)
     GET  /daily/<YYYY-MM-DD>    读取,原样返回入库的 HTML
     GET  /healthz               存活探针
     GET  /                      服务说明(静态页,供探针与误访兜底)

设计要点:**读路径零加工** —— 库里存的就是渲染器原样输出,读出即返回,没有模板拼装、
没有 HTML 解析、没有字符串改写。因此写接口的鉴权是唯一防线,见 security.py。
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date as date_cls
from functools import lru_cache

from django.conf import settings
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from . import security
from .models import Report

logger = logging.getLogger("wxcloudrun")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _ok(data: dict, status: int = 200) -> JsonResponse:
    return JsonResponse(
        {"ok": True, "data": data}, status=status, json_dumps_params={"ensure_ascii": False}
    )


def _err(code: str, message: str, status: int) -> JsonResponse:
    return JsonResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status=status,
        json_dumps_params={"ensure_ascii": False},
    )


def _ip_headers() -> tuple[str, ...]:
    configured = getattr(settings, "REPORT_IP_HEADERS", None)
    return tuple(configured) if configured else security.DEFAULT_IP_HEADERS


@lru_cache(maxsize=16)
def _parse_allowlist(entries: tuple[str, ...]):
    """解析白名单并缓存。键是环境变量解析出的元组,配置不变就不会重复解析。"""
    return security.parse_networks(entries)


@lru_cache(maxsize=8)
def _limiter_for(limit: int) -> security.RateLimiter:
    return security.RateLimiter(limit, window_seconds=60.0)


def reset_caches() -> None:
    """清掉按配置缓存的派生对象。测试在改配置后调用它。"""
    _parse_allowlist.cache_clear()
    _limiter_for.cache_clear()


def _public_base_url(request) -> str:
    """对外可访问的站点根地址。

    优先用显式配置的 ``REPORT_PUBLIC_BASE_URL``:网关转发时的 Host 头不一定是公开域名,
    而返回的 URL 会被写进公众号草稿的「阅读原文」——**错了就是死链**。
    """
    configured = (getattr(settings, "REPORT_PUBLIC_BASE_URL", "") or "").strip()
    if configured:
        return configured.rstrip("/")
    return request.build_absolute_uri("/").rstrip("/")


def _report_data(request, report_date, *, created: bool, unchanged: bool, byte_size: int) -> dict:
    return {
        "date": report_date.isoformat(),
        "url": f"{_public_base_url(request)}/daily/{report_date.isoformat()}",
        "created": created,
        "bytes": byte_size,
        "unchanged": unchanged,
    }


# --------------------------------------------------------------------------
# 写接口
# --------------------------------------------------------------------------


def report_api(request):
    """``POST /api/report`` —— 唯一写接口。

    检查顺序是刻意的:**先 IP 白名单,再密钥**。白名单能挡掉绝大多数扫描流量,
    让密钥校验只面对可信来源,减少爆破面。
    """
    if request.method != "POST":
        response = _err("method_not_allowed", "该接口只接受 POST", 405)
        response["Allow"] = "POST"
        return response

    headers = _ip_headers()
    ips = security.candidate_ips(request, headers)
    logger.info(
        "POST /api/report candidates=%s observed=%s",
        ips,
        security.observed_headers(request, headers),
    )

    # ---- 1) 配置检查:未配置就拒绝,而不是放行(fail-closed)----
    expected_key = (getattr(settings, "REPORT_API_KEY", "") or "").strip()
    if not expected_key:
        return _err(
            "api_key_not_configured",
            "服务端未配置 REPORT_API_KEY,已拒绝写入",
            503,
        )

    entries = tuple(getattr(settings, "REPORT_ALLOWED_IPS", None) or ())
    if not entries:
        return _err(
            "allowlist_not_configured",
            "服务端未配置 REPORT_ALLOWED_IPS,已拒绝写入",
            503,
        )
    networks, invalid = _parse_allowlist(entries)
    if invalid:
        # 白名单里写错一个字符就会让上传被 403,必须让它可见
        logger.error("REPORT_ALLOWED_IPS 存在无法解析的项,已忽略:%s", invalid)

    # ---- 2) IP 白名单 ----
    if not security.ip_allowed(ips, networks):
        logger.warning("拒绝写入:来源 IP 不在白名单 candidates=%s", ips)
        return _err("forbidden_ip", "来源 IP 不在白名单", 403)

    # ---- 3) 限流(按来源 IP)----
    throttle_key = ips[0] if ips else "unknown"
    if not _limiter_for(int(getattr(settings, "REPORT_RATE_LIMIT", 30))).allow(throttle_key):
        logger.warning("拒绝写入:触发限流 candidates=%s", ips)
        return _err("rate_limited", "请求过于频繁", 429)

    # ---- 4) 共享密钥(恒定时间比较)----
    if not security.key_matches(request.headers.get("X-Api-Key", ""), expected_key):
        logger.warning("拒绝写入:密钥不正确 candidates=%s", ips)
        return _err("unauthorized", "密钥不正确", 401)

    # ---- 5) 体积 ----
    max_bytes = int(getattr(settings, "REPORT_MAX_BODY_BYTES", security.MAX_BODY_BYTES))
    declared = (request.META.get("CONTENT_LENGTH") or "").strip()
    if declared.isdigit() and int(declared) > max_bytes:
        return _err("payload_too_large", f"请求体超过 {max_bytes} 字节", 413)

    raw = request.body
    if len(raw) > max_bytes:
        return _err("payload_too_large", f"请求体超过 {max_bytes} 字节", 413)

    # ---- 6) JSON ----
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _err("invalid_body", "请求体必须是 UTF-8 编码的 JSON 对象", 400)
    if not isinstance(payload, dict):
        return _err("invalid_body", "请求体必须是 JSON 对象", 400)

    # ---- 7) 日期 ----
    date_text = payload.get("date")
    if not isinstance(date_text, str) or not security.DATE_RE.match(date_text):
        return _err("invalid_date", "date 必须是 YYYY-MM-DD 格式的字符串", 400)
    try:
        report_date = date_cls.fromisoformat(date_text)
    except ValueError:
        return _err("invalid_date", f"{date_text} 不是合法日期", 400)

    # ---- 8) HTML 内容审计 ----
    html = payload.get("html")
    if not isinstance(html, str):
        return _err("content_rejected", "html 必须是字符串", 422)
    problems = security.audit_html(html)
    if problems:
        logger.warning("拒绝写入 date=%s 未通过内容校验:%s", date_text, problems[:5])
        return _err("content_rejected", "；".join(problems[:5]), 422)

    title = payload.get("title")
    title = title.strip()[:255] if isinstance(title, str) else ""
    summary = payload.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""

    raw_html = html.encode("utf-8")
    digest = hashlib.sha256(raw_html).hexdigest()
    size = len(raw_html)

    # ---- 9) 幂等:内容没变就不写库 ----
    existing = Report.objects.filter(pk=report_date).first()
    if existing is not None and existing.sha256 == digest and existing.byte_size == size:
        logger.info("date=%s 内容未变化,跳过写入", date_text)
        return _ok(
            _report_data(request, report_date, created=False, unchanged=True, byte_size=size)
        )

    with transaction.atomic():
        Report.objects.update_or_create(
            report_date=report_date,
            defaults={
                "title": title,
                "summary": summary,
                "html": html,
                "byte_size": size,
                "sha256": digest,
            },
        )

    created = existing is None
    logger.info("写入成功 date=%s created=%s bytes=%d", date_text, created, size)
    return _ok(
        _report_data(request, report_date, created=created, unchanged=False, byte_size=size),
        status=201 if created else 200,
    )


# --------------------------------------------------------------------------
# 读路径
# --------------------------------------------------------------------------


def daily(request, date: str):
    """``GET /daily/<YYYY-MM-DD>`` —— 原样返回入库的 HTML。"""
    if request.method not in ("GET", "HEAD"):
        response = _err("method_not_allowed", "该接口只接受 GET", 405)
        response["Allow"] = "GET, HEAD"
        return response

    try:
        report_date = date_cls.fromisoformat(date)
    except ValueError:
        return _not_found(request, date)

    report = Report.objects.filter(pk=report_date).first()
    if report is None:
        return _not_found(request, date)

    etag = f'"{report.sha256}"' if report.sha256 else ""
    if etag and request.headers.get("If-None-Match") == etag:
        response = HttpResponse(status=304)
        response["ETag"] = etag
        return response

    # 零加工:charset 与内容里的 <meta charset='utf-8'> 一致,中文不会乱码。
    response = HttpResponse(report.html, content_type="text/html; charset=utf-8")
    if etag:
        response["ETag"] = etag
    response["Cache-Control"] = "public, max-age=300"
    return response


def _not_found(request, date_text: str):
    return render(request, "not_found.html", {"date": date_text}, status=404)


def page_not_found(request, exception=None):
    """全局 404:``/api/*`` 给 JSON 信封,其余给人看的页面。"""
    if request.path.startswith("/api/"):
        return _err("not_found", "接口不存在", 404)
    return _not_found(request, "")


# --------------------------------------------------------------------------
# 探针与首页
# --------------------------------------------------------------------------


def healthz(request):
    """存活探针。

    刻意**不查数据库**:这是存活检查而不是就绪检查 —— 若把 DB 故障也算不健康,
    平台会不停重启容器,反而让恢复更慢。
    """
    return HttpResponse("ok", content_type="text/plain; charset=utf-8")


def home(request):
    """服务说明页。不含任何数据库内容,纯静态模板。"""
    return render(request, "index.html")
