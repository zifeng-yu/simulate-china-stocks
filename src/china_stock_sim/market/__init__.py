"""独立数据层：显式采集、持久化与只读事实服务，不调用模型或账户。"""

from china_stock_sim.market.contracts import DataRequest, DataResult, PendingEventRange
from china_stock_sim.market.service import MarketDataService

__all__ = ["DataRequest", "DataResult", "PendingEventRange", "MarketDataService"]
