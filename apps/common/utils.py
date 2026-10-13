from decimal import Decimal


def num(value):
    """Decimal 을 JSON 친화적인 숫자로. 350.00 -> 350, 12.50 -> 12.5"""
    if value is None:
        return 0
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value
