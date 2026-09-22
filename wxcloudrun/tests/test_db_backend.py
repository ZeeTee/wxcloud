"""MySQL 5.7 兼容后端与后端选择逻辑的测试。

重点是一条**防止误开**的断言:默认配置里必须还是 Django 标准后端。
那个兼容开关是为了绕过 Django 的版本门禁,属于"知情妥协",
绝不能被无意中变成默认值。
"""

from __future__ import annotations

from django.test import SimpleTestCase

from wxcloudrun.db_backend import (
    COMPAT_ENGINE,
    STANDARD_ENGINE,
    engine_name,
)
from wxcloudrun.db_backend.base import (
    MARIADB_MIN_VERSION,
    MYSQL_MIN_VERSION,
    DatabaseFeatures,
    DatabaseWrapper,
)


def _features_for(is_mariadb: bool) -> DatabaseFeatures:
    """构造一个只带 connection 的最小实例,用来读 cached_property。"""
    features = DatabaseFeatures.__new__(DatabaseFeatures)
    features.connection = type("FakeConnection", (), {"mysql_is_mariadb": is_mariadb})()
    return features


class EngineSelectionTests(SimpleTestCase):
    def test_默认走_Django_标准后端(self):
        self.assertEqual(engine_name(False), STANDARD_ENGINE)
        self.assertEqual(STANDARD_ENGINE, "django.db.backends.mysql")

    def test_打开开关才走兼容后端(self):
        self.assertEqual(engine_name(True), COMPAT_ENGINE)

    def test_默认没有启用兼容后端(self):
        """⚠️ 关键防线:兼容后端是知情妥协,不能成为默认值。

        注意要检查**生产** settings 模块,而不是 django.conf.settings ——
        settings_test.py 会把 DATABASES 覆盖成 SQLite,在那里断言 ENGINE 没有意义。
        """
        from wxcloudrun import settings as prod_settings

        self.assertFalse(prod_settings._ALLOW_MYSQL_57)
        self.assertEqual(
            prod_settings.DATABASES["default"]["ENGINE"],
            "django.db.backends.mysql",
        )


class CompatBackendTests(SimpleTestCase):
    def test_兼容后端继承标准后端(self):
        from django.db.backends.mysql.base import (
            DatabaseWrapper as MySQLDatabaseWrapper,
        )

        self.assertTrue(issubclass(DatabaseWrapper, MySQLDatabaseWrapper))

    def test_MySQL_下限被降到_5_7(self):
        self.assertEqual(MYSQL_MIN_VERSION, (5, 7))
        self.assertEqual(_features_for(False).minimum_database_version, (5, 7))

    def test_MariaDB_下限保持上游的值(self):
        """放宽只为 MySQL 5.7;MariaDB 不该跟着变。"""
        self.assertEqual(_features_for(True).minimum_database_version, MARIADB_MIN_VERSION)
        self.assertEqual(MARIADB_MIN_VERSION, (10, 5))

    def test_5_7_18_能通过版本门禁(self):
        """复现部署时报错的那个版本号,确认现在不再被拒。"""
        minimum = _features_for(False).minimum_database_version
        self.assertGreaterEqual((5, 7, 18), minimum)

    def test_兼容后端用的确实是这个_features(self):
        self.assertIs(DatabaseWrapper.features_class, DatabaseFeatures)
