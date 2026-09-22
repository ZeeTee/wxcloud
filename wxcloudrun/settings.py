"""Django settings —— 日报归档服务。

改造自微信云托管官方模板。模板里有三处默认值对**公网服务**是危险的,
本文件是唯一改动它们的地方,不要改回去:

  * ``DEBUG``         原本硬编码 ``True``  —— 生产环境会向访问者泄露堆栈、配置、SQL
  * ``SECRET_KEY``    原本硬编码,而本仓库是**公开**的 —— 等于密钥人人皆知
  * ``ALLOWED_HOSTS`` 原本 ``['*']``

另外模板把日志写进容器内的 ``logs/*.log``。容器文件系统是临时的、且
``container.config.json`` 已声明 ``customLogs: stdout``,所以这里只保留控制台输出,
日志由云托管收集。
"""

import os
import secrets

from .db_backend import engine_name

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _env_flag(name: str, default: str = "0") -> bool:
    """把环境变量解析成布尔。只认 1/true/yes/on(不区分大小写)。"""
    return (os.environ.get(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str) -> list[str]:
    """逗号/分号分隔的环境变量 → 去空白的列表。"""
    raw = os.environ.get(name, "") or ""
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


DEBUG = _env_flag("DJANGO_DEBUG", "0")

# 未配置时随机生成:本服务不使用 session / cookie / 签名,所以密钥无需跨重启稳定。
# 用随机值而不是写死,是为了不把可预测的值带进生产。
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY") or secrets.token_urlsafe(50)

ALLOWED_HOSTS = _env_list("DJANGO_ALLOWED_HOSTS") or ["*"]

# --------------------------------------------------------------------------
# 应用与中间件
# --------------------------------------------------------------------------

INSTALLED_APPS = [
    # 只需要 contenttypes 之外的最小集合:本服务没有用户、会话、后台、静态文件。
    # 连 contenttypes 都不装,迁移就只会建我们自己那一张表。
    "wxcloudrun",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # 没有会话、没有 cookie、没有 CSRF 令牌端点,故不启用 Session/Csrf/Auth 中间件。
    # 写接口用共享密钥 + IP 白名单鉴权,不依赖 cookie —— 也就不存在 CSRF 面。
    # (刻意不加 CsrfViewMiddleware:它会拦下不带 CSRF 令牌的 POST,把写接口打死。)
    "wxcloudrun.middleware.SecurityHeadersMiddleware",
]

ROOT_URLCONF = "wxcloudrun.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "wxcloudrun.wsgi.application"

# --------------------------------------------------------------------------
# 数据库(由云托管注入 MYSQL_* 环境变量)
# --------------------------------------------------------------------------


def _mysql_address() -> tuple[str, str]:
    """``MYSQL_ADDRESS`` 形如 ``host:port``;端口缺失时退回 3306。"""
    raw = (os.environ.get("MYSQL_ADDRESS") or "").strip()
    if not raw:
        return "", "3306"
    host, _, port = raw.partition(":")
    return host, (port or "3306")


_db_host, _db_port = _mysql_address()

#: ⚠️ 逃生开关,默认关闭。
#: 微信云托管**模板一键部署开出来的 MySQL 默认是 5.7**,而 Django 从 4.2 起要求
#: 8.0.11+,于是容器会在启动时直接抛 NotSupportedError。
#: **正解是换到 MySQL 8.0**(见 README「数据库」一节);
#: 只有在无法销毁重建数据库时才打开这个开关,详情与风险见 wxcloudrun/db_backend/。
_ALLOW_MYSQL_57 = _env_flag("DJANGO_ALLOW_MYSQL_57", "0")

DATABASES = {
    "default": {
        "ENGINE": engine_name(_ALLOW_MYSQL_57),
        "NAME": os.environ.get("MYSQL_DATABASE", "django_demo"),
        "USER": os.environ.get("MYSQL_USERNAME", ""),
        "PASSWORD": os.environ.get("MYSQL_PASSWORD", ""),
        "HOST": _db_host,
        "PORT": _db_port,
        "OPTIONS": {"charset": "utf8mb4"},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --------------------------------------------------------------------------
# 日报入库接口的配置
# --------------------------------------------------------------------------

#: 写接口的共享密钥。未配置时写接口返回 503 而不是放行(fail-closed)。
REPORT_API_KEY = (os.environ.get("REPORT_API_KEY") or "").strip()

#: 允许写入的来源 IP,逗号分隔,支持单 IP 与 CIDR。未配置时写接口同样 503。
#: ⚠️ 云托管网关会把 REMOTE_ADDR 换成网关出口 IP,真实客户端 IP 在转发头里,
#:    解析逻辑见 security.candidate_ips()。
REPORT_ALLOWED_IPS = _env_list("REPORT_ALLOWED_IPS")

#: 从哪些转发头里取真实 IP(逗号分隔)。留空用 security 里的默认顺序。
#: 部署后建议看一眼日志里记的候选 IP,确认后可以只保留真正生效的那一个。
REPORT_IP_HEADERS = [h.lower() for h in _env_list("REPORT_IP_HEADERS")]

#: 对外可访问的站点根地址,形如 ``https://xxx.ap-shanghai.run.tcloudbase.com``。
#: **强烈建议配置** —— 写接口返回的 URL 会被写进公众号草稿的「阅读原文」,
#: 而网关转发过来的 Host 头不一定是公开域名,靠它拼出来的可能是死链。
REPORT_PUBLIC_BASE_URL = (os.environ.get("REPORT_PUBLIC_BASE_URL") or "").strip().rstrip("/")

#: 请求体上限(字节)。日报实测约 23KB,这里留 10 倍余量。
REPORT_MAX_BODY_BYTES = int(os.environ.get("REPORT_MAX_BODY_BYTES", str(256 * 1024)))

#: 写接口限流:每个来源 IP 每分钟允许的请求数(正常每天 1 次)。
REPORT_RATE_LIMIT = int(os.environ.get("REPORT_RATE_LIMIT", "30"))

# 让 Django 自己在更早的一层拒绝超大请求(与上面的显式检查互为补充)。
DATA_UPLOAD_MAX_MEMORY_SIZE = REPORT_MAX_BODY_BYTES
FILE_UPLOAD_MAX_MEMORY_SIZE = REPORT_MAX_BODY_BYTES

# --------------------------------------------------------------------------
# 安全响应头
# --------------------------------------------------------------------------

# 容器在网关之后,HTTPS 由网关终结;此开关让 Django 正确识别请求是安全的。
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "no-referrer"
X_FRAME_OPTIONS = "DENY"

# HSTS:本站只经网关以 HTTPS 提供服务,告诉浏览器以后别再走 http。
# 取 1 天且**不开** includeSubDomains / preload —— 这是腾讯云域名,
# 开大了影响面不受我们控制,而收益很小。
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "86400"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

# 刻意保持 False:HTTPS 由云托管网关终结,容器内看到的永远不是"原始 https",
# 在这里做跳转只会死循环,还会把平台的健康检查打成 301。
SECURE_SSL_REDIRECT = False

# --------------------------------------------------------------------------
# 日志(仅控制台;容器里写文件没有意义)
# --------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "WARNING", "propagate": False},
        "wxcloudrun": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# --------------------------------------------------------------------------
# 国际化
# --------------------------------------------------------------------------

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = False
# 用 True:Django 5 的默认值,也是不会被移除的那条路。时间戳按 UTC 存、按
# TIME_ZONE 展示。本服务不展示时间戳,但没必要为此关掉它。
USE_TZ = True

STATIC_URL = "/static/"
