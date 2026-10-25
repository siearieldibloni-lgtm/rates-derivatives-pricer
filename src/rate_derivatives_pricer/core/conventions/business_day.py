"""Business day conventions: how a date falling on a non-business day is moved."""

from __future__ import annotations

from enum import StrEnum


class BusinessDayConvention(StrEnum):
    """Rule used to adjust a non-business day (ISDA 2006 definitions, section 4.12).

    Values are the stable identifiers used in configuration files and in the API.
    """

    #: Move to the next business day.
    FOLLOWING = "FOLLOWING"
    #: Move to the next business day, unless it falls in the next calendar month, in which case
    #: move to the previous business day.
    MODIFIED_FOLLOWING = "MODIFIED_FOLLOWING"
    #: Move to the previous business day.
    PRECEDING = "PRECEDING"
    #: Do not adjust.
    UNADJUSTED = "UNADJUSTED"
