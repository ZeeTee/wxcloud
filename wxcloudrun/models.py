"""数据模型。

只有一张表:一天一条日报。模板自带的 ``Counters`` 计数器 demo 已移除。
"""

from __future__ import annotations

from django.db import models


class MediumTextField(models.TextField):
    """MySQL 下建为 ``MEDIUMTEXT``(16MB 上限),其他后端退回 ``TEXT``。

    Django 没有内置的 MEDIUMTEXT。日报一天约 23KB、一年约 8MB,MEDIUMTEXT 足够,
    且比 ``LONGTEXT``(4GB)更贴合意图 —— 万一写入方失控也不至于把库撑到离谱。
    """

    def db_type(self, connection):
        if connection.vendor == "mysql":
            return "mediumtext"
        return "text"


class Report(models.Model):
    """一天的日报。

    ``report_date`` 直接做主键:一天只有一份日报,重复上传就是**更新同一条**,
    天然幂等,不会堆垃圾记录。
    """

    report_date = models.DateField(primary_key=True, verbose_name="日报日期")

    # title / summary 是**元数据**,页面不用(本站没有列表页)。
    # 留着是为了排障时 `SELECT report_date, title FROM reports` 能一眼看出哪条是哪天的。
    title = models.CharField(max_length=255, blank=True, default="", verbose_name="标题")
    summary = models.TextField(blank=True, default="", verbose_name="一句话综述")

    # 渲染器原样输出的完整 HTML 文档(公众号草稿与网页共用同一份)。
    html = MediumTextField(verbose_name="日报 HTML")

    byte_size = models.PositiveIntegerField(default=0, verbose_name="UTF-8 字节数")
    sha256 = models.CharField(max_length=64, blank=True, default="", verbose_name="内容指纹")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    updated_at = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "reports"
        ordering = ["-report_date"]
        verbose_name = "日报"
        verbose_name_plural = "日报"

    def __str__(self) -> str:
        return f"{self.report_date} {self.title}"[:100]
