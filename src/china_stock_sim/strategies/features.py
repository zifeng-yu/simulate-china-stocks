"""Deterministic Decimal signal features; no market reads or missing-value filling."""

from decimal import Decimal, localcontext

from china_stock_sim.common.fees import integer, number


def sample_volatility(prices):
    """Sample standard deviation of successive simple returns, not annualized."""
    with localcontext() as ctx:
        ctx.prec = 34
        values = [number(value, positive=True) for value in prices]
        if len(values) < 3:
            raise ValueError("样本波动率至少需要3个连续价格")
        returns = [b / a - 1 for a, b in zip(values, values[1:])]
        mean = sum(returns) / Decimal(len(returns))
        return (sum((value - mean) ** 2 for value in returns) / (len(returns) - 1)).sqrt()


def rule_features(prices, lookback_sessions=121, momentum_skip_sessions=20,
                  volatility_sessions=60):
    with localcontext() as ctx:
        ctx.prec = 34
        length = integer(lookback_sessions, minimum=2)
        skip = integer(momentum_skip_sessions)
        window = integer(volatility_sessions, minimum=2)
        if length < skip + 2 or window + 1 > length or len(prices) != length:
            raise ValueError("评分价格数量或窗口不匹配")
        values = [number(value, positive=True) for value in prices]
        return {"momentum": values[length - 1 - skip] / values[0] - 1,
                "volatility": sample_volatility(values[-window - 1:])}


def rank_features(features, momentum_weight="0.5", low_volatility_weight="0.5"):
    """Midrank percentiles among valid candidates, then full score/code ordering."""
    with localcontext() as ctx:
        ctx.prec = 34
        mw, vw = number(momentum_weight), number(low_volatility_weight)
        if mw + vw != 1:
            raise ValueError("评分权重之和必须为1")
        rows, size = [], len(features)
        for code, item in features.items():
            # Momentum may legitimately be negative; features originate in rule_features.
            momentum = item["momentum"]
            if not isinstance(momentum, Decimal) or not momentum.is_finite():
                raise ValueError("动量须为有限Decimal")
            volatility = number(item["volatility"])
            if size == 1:
                mp = vp = Decimal("0.5")
            else:
                lower = sum(other["momentum"] < momentum for other in features.values())
                equal_m = sum(other["momentum"] == momentum for other in features.values())
                higher = sum(other["volatility"] > volatility for other in features.values())
                equal_v = sum(other["volatility"] == volatility for other in features.values())
                mp = (Decimal(lower) + Decimal(equal_m - 1) / 2) / (size - 1)
                vp = (Decimal(higher) + Decimal(equal_v - 1) / 2) / (size - 1)
            rows.append({"code": code, "momentum": momentum, "volatility": volatility,
                         "momentum_percentile": mp, "low_volatility_percentile": vp,
                         "score": mw * mp + vw * vp})
        rows.sort(key=lambda row: (-row["score"], row["code"]))
        return [{**row, "rank": index + 1} for index, row in enumerate(rows)]
