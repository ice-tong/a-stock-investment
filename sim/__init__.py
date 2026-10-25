"""A 股模拟交易系统（第一阶段：确定性内核）。用法见 sim/__main__.py。"""
import os

# 数据源全在国内，Clash/mihomo 对国内域名本来就走 DIRECT；显式绕过系统代理，
# 避免代理端口（如 127.0.0.1:49153）还开着但上游不通时，整批取数变成 ProxyError。
os.environ.setdefault("NO_PROXY", "*")
