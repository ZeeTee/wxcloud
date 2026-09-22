"""可选的 MySQL 5.7 兼容后端(默认**不启用**)。

这个包存在的唯一原因:微信云托管**模板一键部署开出来的 MySQL 默认是 5.7**,
而 Django 从 4.2 起把最低版本提到 8.0.11,于是容器一启动就:

    django.db.utils.NotSupportedError: MySQL 8.0.11 or later is required (found 5.7.18)

**正解是换到 MySQL 8.0**(5.7 不能原地升级,需在控制台「销毁数据库」后重新开通,
详见 README)。本包只是"不想销毁数据库"时的退路,用环境变量
``DJANGO_ALLOW_MYSQL_57=1`` 打开。

⚠️ 打开它等于跑在 Django **明确不支持、也未测试过**的组合上。它对本项目是安全的,
因为这里只用到 MySQL 5.7 完全支持的 SQL(DATE 主键、VARCHAR/TEXT、INT UNSIGNED、
DATETIME(6)、基础 CRUD);Django 其余能力探测都按真实版本号判断,8.0 专属特性
(如 CHECK 约束、``SKIP LOCKED``)会自动关闭。但不要在这之上继续叠加新特性。

本模块刻意不 import 任何 Django 的东西 —— 它会被 settings.py 在 Django 初始化**之前**
导入。真正需要 Django 的部分在 ``base.py``。
"""

from __future__ import annotations

#: Django 自带的 MySQL 后端
STANDARD_ENGINE = "django.db.backends.mysql"

#: 本包的兼容后端(路径形式,给 settings.DATABASES["default"]["ENGINE"] 用)
COMPAT_ENGINE = "wxcloudrun.db_backend"


def engine_name(allow_mysql_57: bool) -> str:
    """按开关选择数据库后端。"""
    return COMPAT_ENGINE if allow_mysql_57 else STANDARD_ENGINE
