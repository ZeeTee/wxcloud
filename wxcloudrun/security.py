"""安全相关的纯函数与工具。

单独成文件是为了**能不依赖 Django 请求对象单测**:这些逻辑是本服务的全部防线,
必须可被直接验证。

设计前提(重要):云托管网关会把 ``REMOTE_ADDR`` 换成**网关出口 IP**,真实客户端 IP
只存在于转发头里。腾讯云文档明确指出 ``X-Forwarded-For`` / ``X-Real-IP`` 可能拿不到
真实 IP,建议改用 ``X-Original-Forwarded-For``。因此这里**同时**检查多个头,并把
观察到的候选 IP 全部记进日志 —— 部署后看一眼日志即可确认到底哪个头生效。

⚠️ 由于"哪个头可信"取决于平台行为、无法在本地验证,IP 校验被定位为**纵深防御**:
共享密钥才是主防线,IP 白名单用来挡掉绝大多数扫描流量。两者都必需。
"""

from __future__ import annotations

import hmac
import ipaddress
import re
import threading
import time
from html.parser import HTMLParser

# --------------------------------------------------------------------------
# 体积与格式上限
# --------------------------------------------------------------------------

#: 请求体上限(字节)。日报实测约 23KB。
MAX_BODY_BYTES = 256 * 1024

#: 日期必须是严格的 YYYY-MM-DD(先卡格式,再交给 datetime 验证是不是真实日期)。
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 完整 HTML 文档必须以 DOCTYPE 开头 —— 这是"入库的是完整文档"这一契约的机器校验。
_DOCTYPE_PREFIX = "<!doctype html"

# --------------------------------------------------------------------------
# HTML 审计
# --------------------------------------------------------------------------

#: 整类禁用的标签。它们要么能执行脚本,要么能发起外部请求。
DENY_TAGS = frozenset(
    {
        "script",
        "iframe",
        "object",
        "embed",
        "applet",
        "frame",
        "frameset",
        "form",
        "base",
        "link",
        "noscript",
        "template",
        "svg",
        "math",
    }
)

#: 原文里出现即拒的子串。粗筛,与下面的结构化检查互为补充。
DENY_SUBSTRINGS = (
    "<script",
    "<iframe",
    "<object",
    "<embed",
    "<applet",
    "javascript:",
    "vbscript:",
    "data:text/html",
)

#: 会触发网络请求或跳转的属性名 —— 它们的协议要单独校验。
URL_ATTRS = frozenset(
    {"href", "src", "action", "formaction", "background", "poster", "data", "xlink:href"}
)

#: 允许的 URL 协议。空字符串代表相对路径 / 锚点。刻意**不允许** data:,
#: 因为 data:image/svg+xml 可以携带脚本。
ALLOWED_URL_SCHEMES = frozenset({"", "http", "https", "mailto"})


def url_scheme(value: str) -> str:
    """取出 URL 的协议名;不是"协议:..."写法时返回空串。

    浏览器解析协议时会**忽略其中的控制字符与空白**(``java\\tscript:`` 仍是 javascript),
    所以这里在判断前先去掉它们,否则会被轻易绕过。
    """
    text = (value or "").strip()
    before = text.split(":", 1)[0] if ":" in text else text
    cleaned = re.sub(r"[\x00-\x20]", "", before).lower()
    if not cleaned or not re.fullmatch(r"[a-z][a-z0-9+.\-]*", cleaned):
        return ""
    return cleaned


class _HtmlAuditor(HTMLParser):
    """结构化审计:逐个标签检查标签名、事件属性、URL 协议。

    之所以不用裸正则查 ``on\\w+=``:正文文本里完全可能出现 ``only=`` 这类字样,
    裸正则会把正常内容误判成事件属性。按**标签属性**解析才能区分。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.problems: list[str] = []

    def _check(self, tag: str, attrs) -> None:
        name = (tag or "").lower()
        if name in DENY_TAGS:
            self.problems.append(f"不允许的标签 <{name}>")

        for raw_name, raw_value in attrs:
            attr = (raw_name or "").lower()
            value = raw_value or ""

            if attr.startswith("on"):
                self.problems.append(f"不允许的事件属性 {attr}=")
                continue

            if attr == "http-equiv" and value.strip().lower() == "refresh":
                self.problems.append("不允许的 <meta http-equiv=refresh>")
                continue

            if attr == "style" and "expression(" in value.lower():
                self.problems.append("不允许的 CSS expression()")
                continue

            if attr in URL_ATTRS:
                scheme = url_scheme(value)
                if scheme not in ALLOWED_URL_SCHEMES:
                    self.problems.append(f"{attr} 使用了不允许的协议 {scheme}:")

    def handle_starttag(self, tag, attrs):
        self._check(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._check(tag, attrs)


def audit_html(html: str) -> list[str]:
    """审计待入库的 HTML,返回问题列表;空列表代表通过。

    这是纯粹的"坏东西黑名单"。它不追求把 HTML 变干净(不做清洗、不做改写),
    只回答一个问题:**这份内容能不能原样发布到我们的域名下。**
    """
    problems: list[str] = []

    if not html:
        return ["内容为空"]
    if not html.lstrip()[: len(_DOCTYPE_PREFIX)].lower().startswith(_DOCTYPE_PREFIX):
        problems.append("必须是完整 HTML 文档(以 <!DOCTYPE html> 开头)")

    lowered = html.lower()
    for bad in DENY_SUBSTRINGS:
        if bad in lowered:
            problems.append(f"包含不允许的内容:{bad}")

    auditor = _HtmlAuditor()
    try:
        auditor.feed(html)
        auditor.close()
    except Exception as exc:  # 解析器对畸形输入抛错 → 直接判为不可信
        problems.append(f"HTML 解析失败:{exc.__class__.__name__}")
    problems.extend(auditor.problems)

    # 去重且保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for item in problems:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


# --------------------------------------------------------------------------
# 客户端 IP
# --------------------------------------------------------------------------

#: 默认检查的转发头,按可信度从高到低。可用环境变量 REPORT_IP_HEADERS 覆盖。
DEFAULT_IP_HEADERS = (
    "x-original-forwarded-for",
    "x-forwarded-for",
    "x-real-ip",
    "x-wx-client-ip",
)


def normalize_ip(raw: str) -> str:
    """归一化单个 IP 字面量;不是合法 IP 时返回空串。

    兼容 ``1.2.3.4:80`` 与 ``[::1]:80`` 这类带端口的写法。
    """
    text = (raw or "").strip()
    if not text:
        return ""

    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            text = text[1:end]
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
        if port.isdigit():
            text = host

    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def candidate_ips(request, headers=None) -> list[str]:
    """可能代表真实客户端的 IP 列表(去重、保序)。

    ``X-Forwarded-For`` 可能有多段。**只取最右一段** —— 它是最近一跳代理写入的;
    左边的段由客户端提供,可以随意伪造。其余头是单值头,同样取最右一段。
    """
    names = headers or DEFAULT_IP_HEADERS
    found: list[str] = []

    for name in names:
        meta_key = "HTTP_" + name.upper().replace("-", "_")
        raw = request.META.get(meta_key) or ""
        parts = [p for p in raw.split(",") if p.strip()]
        if not parts:
            continue
        ip = normalize_ip(parts[-1])
        if ip and ip not in found:
            found.append(ip)

    remote = normalize_ip(request.META.get("REMOTE_ADDR") or "")
    if remote and remote not in found:
        found.append(remote)

    return found


def observed_headers(request, headers=None) -> dict[str, str]:
    """收集所有转发头的原始值,仅用于日志排障(判断平台到底发了哪个头)。"""
    names = headers or DEFAULT_IP_HEADERS
    snapshot: dict[str, str] = {}
    for name in names:
        raw = request.META.get("HTTP_" + name.upper().replace("-", "_"))
        if raw:
            snapshot[name] = raw
    remote = request.META.get("REMOTE_ADDR")
    if remote:
        snapshot["remote_addr"] = remote
    return snapshot


def parse_networks(entries) -> tuple[list, list[str]]:
    """把 IP / CIDR 字符串列表解析成网络对象列表。

    返回 ``(网络列表, 无法解析的原始项)``。调用方应对后者**报警**而不是静默忽略 ——
    白名单里写错一个字符就会让上传被 403,必须让它可见。
    """
    networks = []
    invalid = []
    for entry in entries or []:
        text = (entry or "").strip()
        if not text:
            continue
        try:
            networks.append(ipaddress.ip_network(text, strict=False))
        except ValueError:
            invalid.append(text)
    return networks, invalid


def ip_allowed(ips, networks) -> bool:
    """任一候选 IP 落在白名单内即通过。

    用"任一"而不是"全部",是因为无法在本地确定平台究竟把真实 IP 放在哪个头里;
    把所有候选都要求匹配会让正常上传必然失败。密钥是主防线,这里只做过滤。
    """
    if not networks:
        return False
    for text in ips or []:
        try:
            addr = ipaddress.ip_address(text)
        except ValueError:
            continue
        for network in networks:
            if addr.version == network.version and addr in network:
                return True
    return False


# --------------------------------------------------------------------------
# 共享密钥
# --------------------------------------------------------------------------


def key_matches(provided: str, expected: str) -> bool:
    """恒定时间比较,避免通过响应时间逐字节猜密钥。"""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


# --------------------------------------------------------------------------
# 限流(进程内,够用即可)
# --------------------------------------------------------------------------


class RateLimiter:
    """滑动窗口限流。

    单副本、单写入方(每天一次),进程内计数完全够用;不引入 Redis 之类的依赖。
    ``maxNum`` 已配成 1,所以不存在多副本各自计数的问题。
    """

    def __init__(self, limit: int, window_seconds: float = 60.0, max_keys: int = 4096) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window_seconds)
        self.max_keys = max_keys
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            recent = [t for t in self._hits.get(key, ()) if stamp - t < self.window]
            if len(recent) >= self.limit:
                self._hits[key] = recent
                return False
            recent.append(stamp)
            self._hits[key] = recent
            if len(self._hits) > self.max_keys:
                self._evict(stamp)
            return True

    def _evict(self, now: float) -> None:
        """丢弃窗口内已无记录的键,防止被大量不同 IP 撑爆内存。"""
        for key in [k for k, v in self._hits.items() if not any(now - t < self.window for t in v)]:
            self._hits.pop(key, None)
        # 仍然过多时按最旧一次命中淘汰,保证内存有上界
        if len(self._hits) > self.max_keys:
            oldest = sorted(self._hits.items(), key=lambda kv: kv[1][-1] if kv[1] else 0.0)
            for key, _ in oldest[: len(self._hits) - self.max_keys]:
                self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()
