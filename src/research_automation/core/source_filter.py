"""来源可信度过滤：屏蔽中国社交媒体及低质量来源。"""
from __future__ import annotations

# 域名黑名单（link 字段包含这些字符串则过滤）
_DOMAIN_BLACKLIST: frozenset[str] = frozenset([
    "weibo.com",
    "mp.weixin.qq.com",
    "weixin.qq.com",
    "douyin.com",
    "tiktok.com",
    "kuaishou.com",
    "xiaohongshu.com",
    "xhslink.com",
    "zhihu.com",
    "baidu.com/s",
    "sohu.com",
    "sina.com.cn",
    "163.com",
    "qq.com/news",
])

# source 字段黑名单（来源标签包含这些字符串则过滤，不区分大小写）
_SOURCE_BLACKLIST: frozenset[str] = frozenset([
    "weibo",
    "wechat",
    "douyin",
    "xiaohongshu",
    "red note",
    "rednote",
    "tiktok",
    "kuaishou",
    "zhihu",
    "sina",
    "sohu",
    "netease",
    "163.com",
])


def is_trusted_source(article: dict) -> bool:
    """
    返回 True 表示来源可信，可以保留；False 表示应过滤掉。
    检查 link 域名和 source 标签，任一命中黑名单即过滤。
    """
    link = (article.get("link") or article.get("url") or "").lower().strip()
    source = (article.get("source") or "").lower().strip()

    for domain in _DOMAIN_BLACKLIST:
        if domain in link:
            return False

    for src in _SOURCE_BLACKLIST:
        if src in source:
            return False

    return True


def filter_trusted(articles: list[dict]) -> list[dict]:
    """批量过滤，返回可信来源的文章列表。"""
    return [a for a in articles if is_trusted_source(a)]
