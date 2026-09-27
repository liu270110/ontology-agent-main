"""B4 出口白名单（fail-closed）：web 能力唯一合法出口判定面（docs/Agent/06 #3「白名单代理出口」）。

语义（研究整理/08 C3「出口白名单」同款）：

- **空白名单 = 全拒**（fail-closed 缺省）：未配置出口策略的能力不得触网——默认值论证：
  egress 白名单是安全边界而非功能开关，宽开缺省（如「未配置=放行」）会把「忘配置」变成
  「全开放」，与设计宪法 5（全程可追溯/硬门禁不可跳过）相悖；部署方按租户显式下发域名列。
- 条目匹配 = 精确域名或其子域（``api.example.com`` 命中 ``example.com``）；
  大小写/末点归一后比对，端口不参与判定（域名级白名单）。
- scheme 仅 http/https（SSRF 最小关断：file/ftp/gopher 等一律拒绝）。
- 重定向逐跳复检：跨白名单跳转在**请求发出前**拦截（fetch/search 同用本面）。

遗留（P1，研究整理/08 C3 同款）：DNS rebinding/IP 直连钉扎校验需代理解析层配合，
本批不涉（白名单本身已把可触达面压到显式下发集合）。
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")


class EgressAllowlist:
    """域名出口白名单（值语义，不可变）：空白名单=全拒，条目=精确域名或其子域。"""

    __slots__ = ("_domains",)

    def __init__(self, domains: Iterable[str] = ()) -> None:
        normalized: list[str] = []
        for item in domains:
            host = str(item).strip().lower().rstrip(".")
            if host:
                normalized.append(host)
        self._domains = tuple(dict.fromkeys(normalized))  # 去重保序

    def allows_host(self, host: str | None) -> bool:
        """域名判定：精确或子域命中；空/非法一律拒绝。"""
        if not host:
            return False
        host = host.strip().lower().rstrip(".")
        if not host:
            return False
        return any(host == domain or host.endswith("." + domain) for domain in self._domains)

    def allows_url(self, url: str) -> bool:
        """URL 判定：scheme 限 http/https + 域名命中；畸形 URL 一律拒绝。"""
        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            return False
        return self.allows_host(parts.hostname)

    def domains(self) -> tuple[str, ...]:
        """只读视图（审计/装配面用）。"""
        return self._domains

    def __len__(self) -> int:
        return len(self._domains)

    def __repr__(self) -> str:
        return f"EgressAllowlist(domains={self._domains!r})"


def hostname_of(url: str) -> str:
    """URL → 主机名（审计落条用；畸形/无主机返回空串，不抛异常）。"""
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
