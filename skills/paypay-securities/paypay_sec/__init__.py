"""paypay_sec — read-only client for the PayPay証券 web frontend.

Phase 1 scope: authenticate via /login.json and read the SSR pages
(portfolio / balance / history). No write/order operations live here.
"""

__version__ = "0.1.0"

from .mobile_client import (  # noqa: E402
    BffUnauthorized,
    MobileLoginError,
    MobileSettings,
    PayPayMobileClient,
)

__all__ = [
    "PayPayMobileClient",
    "MobileSettings",
    "MobileLoginError",
    "BffUnauthorized",
]
