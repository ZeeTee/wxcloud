"""测试专用 settings:把数据库换成内存 SQLite。

本机没有 MySQL,而 CI / 开发机上跑测试不应该依赖外部服务。
其余配置全部继承自 settings.py —— 包括被测的安全默认值,这一点很重要:
测试必须覆盖真实配置,而不是一份被改软了的副本。
"""

from .settings import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# 测试里密码哈希等无关,加快启动
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# 测试断言 404 页面时不需要 ALLOWED_HOSTS 报错
ALLOWED_HOSTS = ["*"]
