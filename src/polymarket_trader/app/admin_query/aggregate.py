"""聚合所有 admin 查询子 mixin。

最终形态：``class AdminQueryMixin(<sub_mixin_1>, <sub_mixin_2>, ...): pass``。
拆分推进期间，未抽取的方法仍由 ``admin_query_mixin`` 模块的旧类承担；通过
旧类继承本聚合类，将子 mixin 提供的方法注入到 AdminService 现有的继承链中。
"""

from __future__ import annotations


class AdminQueryMixin:
    """admin 只读查询方法聚合。

    每次抽取一个子 mixin 时，将其加入到本类的基类元组；当前为骨架，
    后续 commit 逐步填充。
    """

    pass


__all__ = ["AdminQueryMixin"]
