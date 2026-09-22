"""把 MySQL 最低版本门禁降到 5.7 的兼容后端。

Django 的 ``load_backend()`` 会 import ``<ENGINE>.base`` 并取其中的 ``DatabaseWrapper``,
所以这里只需要继承标准 MySQL 后端、换掉 ``features_class`` 即可 ——
``minimum_database_version`` 是 Django 给后端预留的扩展点,不是私有实现细节。

除了这个门禁,**不做任何其它改动**:Django 的其余能力探测都读真实版本号
(``self.connection.mysql_version``),在 5.7 上会自动退回老路径。
"""

from __future__ import annotations

from django.db.backends.mysql.base import DatabaseWrapper as MySQLDatabaseWrapper
from django.db.backends.mysql.features import DatabaseFeatures as MySQLDatabaseFeatures
from django.utils.functional import cached_property

#: 放宽后的 MySQL 下限。5.7.x 全系列都满足。
MYSQL_MIN_VERSION = (5, 7)

#: MariaDB 保持 Django 上游的值,不跟着放宽。
MARIADB_MIN_VERSION = (10, 5)


class DatabaseFeatures(MySQLDatabaseFeatures):
    """只覆盖最低版本这一项。"""

    @cached_property
    def minimum_database_version(self):
        if self.connection.mysql_is_mariadb:
            return MARIADB_MIN_VERSION
        return MYSQL_MIN_VERSION


class DatabaseWrapper(MySQLDatabaseWrapper):
    features_class = DatabaseFeatures
