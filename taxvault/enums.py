"""Controlled vocabularies. Strings in the database, enums in the code."""

from __future__ import annotations

from enum import Enum


class FilingStatus(str, Enum):
    SINGLE = "single"
    MARRIED_JOINTLY = "married_jointly"
    MARRIED_SEPARATELY = "married_separately"
    HEAD_OF_HOUSEHOLD = "head_of_household"
    QUALIFYING_SURVIVING_SPOUSE = "qualifying_surviving_spouse"


class EstimateMethod(str, Enum):
    """The two modes the client picks between on the estimate screen."""

    REGULAR = "regular"    # the return as the documents stand
    PLANNING = "planning"  # the same year, re-run with optimisations applied


class PlanningStrategy(str, Enum):
    TRADITIONAL_401K = "traditional_401k"
    HSA = "hsa"
    TRADITIONAL_IRA = "traditional_ira"
    DEPENDENT_CARE_FSA = "dependent_care_fsa"
    HEALTH_FSA = "health_fsa"
    ITEMISE_INSTEAD = "itemise_instead"
    BUNCH_CHARITABLE = "bunch_charitable"
    FILING_STATUS_SWITCH = "filing_status_switch"
    QBI = "qbi"
    SAVERS_CREDIT = "savers_credit"
    TAX_LOSS_HARVEST = "tax_loss_harvest"
    ZERO_RATE_GAIN_HARVEST = "zero_rate_gain_harvest"
    HOLD_FOR_LONG_TERM = "hold_for_long_term"
    ROTH_VS_TRADITIONAL = "roth_vs_traditional"
    AVOID_EARLY_WITHDRAWAL = "avoid_early_withdrawal"
    HOME_EQUITY_TRACING = "home_equity_tracing"
    MORTGAGE_POINTS = "mortgage_points"
    RESIDENCY = "residency"
    WITHHOLDING_TUNE = "withholding_tune"
    EDUCATION_CREDIT = "education_credit"


class DocumentKind(str, Enum):
    W2 = "w2"
    F1099_NEC = "1099_nec"
    F1099_INT = "1099_int"
    F1099_DIV = "1099_div"
    F1099_B = "1099_b"
    F1099_R = "1099_r"
    F1099_G = "1099_g"
    F1098_E = "1098_e"
    F1098_T = "1098_t"
    F1098 = "1098"
    K1 = "k1"
    OTHER = "other"


class DocumentStatus(str, Enum):
    UPLOADED = "uploaded"
    PARSED = "parsed"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class FilingState(str, Enum):
    """What we know about a given year and jurisdiction."""

    NOT_FILED = "not_filed"
    FILED = "filed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXTENDED = "extended"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


class Jurisdiction(str, Enum):
    FEDERAL = "federal"
    STATE = "state"


class PaymentDirection(str, Enum):
    REFUND = "refund"
    BALANCE_DUE = "balance_due"
    EVEN = "even"


class PaymentMethod(str, Enum):
    DIRECT_DEPOSIT = "direct_deposit"
    PAPER_CHECK_REFUND = "paper_check_refund"
    DIRECT_DEBIT = "direct_debit"
    IRS_DIRECT_PAY = "irs_direct_pay"
    EFTPS = "eftps"
    CARD = "card"
    CHECK = "check"
    INSTALLMENT_AGREEMENT = "installment_agreement"
    SHORT_TERM_EXTENSION = "short_term_extension"


class VerificationChannel(str, Enum):
    EMAIL = "email"
    SMS = "sms"
