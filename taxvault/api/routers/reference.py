"""Reference data: the states, the years, and the limits.

Public and unauthenticated on purpose. None of it is about a person, and the
sign-in screen needs the state list before anyone has signed in.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from taxvault.config import (
    federal,
    latest_year,
    payments,
    resources,
    states,
    supported_years,
)
from taxvault.engines.retirement import compute_rmd, penalty_exceptions
from taxvault.money import money

router = APIRouter(prefix="/api/reference", tags=["reference"])


@router.get("/years")
def years() -> dict[str, Any]:
    """Which years can be worked out, and which one a client is filing now.

    `current` is the year being FILED, which is not the newest parameter file
    on disk. In September 2026 the return being prepared is tax year 2025 --
    2026 has not finished, so nobody can file it. Shipping the 2026 figures
    must not quietly retarget the app at a year that cannot be filed yet;
    those figures are for planning, and `planning` is where they are offered.
    """
    supported = supported_years()
    this_year = date.today().year
    finished = [y for y in supported if y < this_year]
    current = finished[-1] if finished else latest_year()
    params = federal(current)
    planning = latest_year()
    return {
        "supported": supported,
        "current": current,
        "planning": planning,
        "filing_years": finished or supported,
        "due_date": params.due_date,
        "extended_due_date": params.extended_due_date,
        "source": params.source,
        "note": (
            f"Tax year {current} is the return being filed now. "
            f"Tax year {planning} is open for planning: the year is not over, so it "
            "cannot be filed yet."
            if planning != current else ""
        ),
    }


@router.get("/states")
def state_list(year: int | None = Query(default=None)) -> dict[str, Any]:
    """Every jurisdiction, split the way the client screen shows them."""
    params = states(year)
    entries = []
    for code in params.codes():
        state = params.get(code)
        entry: dict[str, Any] = {
            "code": code,
            "name": state["name"],
            "type": state["type"],
            "has_income_tax": state["type"] != "none",
            "note": state.get("note", ""),
        }
        if state["type"] == "flat":
            entry["rate"] = str(money(state["rate"]))
        elif state["type"] == "graduated":
            schedule = params.brackets(code, "single")
            entry["lowest_rate"] = str(schedule[0].rate)
            entry["top_rate"] = str(schedule[-1].rate)
        if state.get("local_rate_typical"):
            entry["local_rate_typical"] = str(money(state["local_rate_typical"]))
            entry["local_note"] = state.get("local_note", "")
        if state.get("surtax"):
            entry["surtax"] = {
                "rate": str(money(state["surtax"]["rate"])),
                "over": str(money(state["surtax"]["over"])),
                "label": state["surtax"].get("label", ""),
            }
        if state.get("capital_gains_excise"):
            entry["capital_gains_excise"] = state["capital_gains_excise"]
        entries.append(entry)

    no_tax = params.no_tax_states()
    return {
        "tax_year": params.year,
        "disclaimer": params.disclaimer,
        "counts": {
            "total": len(entries),
            "no_income_tax": len(no_tax),
            "flat": len([e for e in entries if e["type"] == "flat"]),
            "graduated": len([e for e in entries if e["type"] == "graduated"]),
        },
        "no_income_tax": no_tax,
        "states": entries,
    }


@router.get("/states/{code}")
def state_detail(code: str, year: int | None = Query(default=None)) -> dict[str, Any]:
    params = states(year)
    try:
        state = params.get(code)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    out = dict(state)
    if state["type"] == "graduated":
        out["schedule_single"] = [
            {"from": str(b.floor), "to": None if b.ceiling is None else str(b.ceiling),
             "rate": str(b.rate)}
            for b in params.brackets(code, "single")
        ]
        out["schedule_joint"] = [
            {"from": str(b.floor), "to": None if b.ceiling is None else str(b.ceiling),
             "rate": str(b.rate)}
            for b in params.brackets(code, "married_jointly")
        ]
    return out


@router.get("/limits")
def limits(year: int | None = Query(default=None)) -> dict[str, Any]:
    """Contribution limits and key thresholds, for the planning screen."""
    params = federal(year)
    return {
        "tax_year": params.year,
        "source": params.source,
        "standard_deduction": params.get("standard_deduction", default={}),
        "contribution_limits": params.get("contribution_limits", default={}),
        "salt_cap": str(params.amount("deductions", "salt_cap")),
        "child_tax_credit": str(params.amount("credits", "child_tax_credit", "amount")),
        "capital_gains": params.get("capital_gains", default={}),
        "capital_losses": params.get("capital_losses", default={}),
        "home_loans": params.get("home_loans", default={}),
        "brackets": {
            status: [
                {"from": str(b.floor), "to": None if b.ceiling is None else str(b.ceiling),
                 "rate": str(b.rate)}
                for b in params.brackets(status)
            ]
            for status in params.statuses
        },
    }


@router.get("/retirement")
def retirement_reference(year: int | None = Query(default=None)) -> dict[str, Any]:
    """Ages, codes and exceptions for the retirement screen.

    The Uniform Lifetime Table is deliberately left out: it is 40 rows the UI
    has no use for, and the RMD endpoint applies it server-side.
    """
    params = federal(year)
    rules = dict(params.get("retirement_distributions", default={}) or {})
    rules.pop("uniform_lifetime_table", None)
    limits = params.get("contribution_limits", default={}) or {}
    return {
        "tax_year": params.year,
        "source": params.source,
        "limits": {
            "elective_deferral": str(params.amount("contribution_limits",
                                                   "elective_deferral_401k")),
            "catch_up": str(params.amount("contribution_limits", "catch_up_401k")),
            "super_catch_up": str(params.amount("contribution_limits",
                                                "super_catch_up_401k")),
            "super_catch_up_ages": limits.get("super_catch_up_ages", []),
            "annual_additions": str(params.amount("contribution_limits",
                                                  "annual_additions_401k")),
            "ira": str(params.amount("contribution_limits", "ira")),
            "ira_catch_up": str(params.amount("contribution_limits", "ira_catch_up")),
        },
        "rules": rules,
        "penalty_exceptions": penalty_exceptions(params),
        "note": (
            "Every exception waives the 10% additional tax only. Income tax is still due "
            "on the distribution."
        ),
    }


@router.get("/rmd")
def rmd(
    birth_year: int = Query(...),
    balance: float = Query(default=0, description="Account value on 31 December last year"),
    taken: float = Query(default=0),
    year: int | None = Query(default=None),
    is_roth_401k: bool = Query(default=False),
    still_working: bool = Query(default=False),
    owns_five_percent: bool = Query(default=False),
) -> dict[str, Any]:
    """Whether a required minimum distribution is due, and what missing it costs."""
    params = federal(year)
    result = compute_rmd(
        params,
        birth_year=birth_year,
        prior_year_balance=Decimal(str(balance)),
        taken=Decimal(str(taken)),
        is_roth_401k=is_roth_401k,
        still_working_for_plan_sponsor=still_working,
        owns_five_percent=owns_five_percent,
    )
    return {"tax_year": params.year, **result.to_dict()}


@router.get("/home-loans")
def home_loan_reference(year: int | None = Query(default=None)) -> dict[str, Any]:
    """The debt ceilings, the grandfather date and whether PMI counts this year."""
    params = federal(year)
    rules = params.get("home_loans", default={}) or {}
    return {
        "tax_year": params.year,
        "source": params.source,
        "rules": rules,
        "mortgage_insurance_deductible": bool(
            rules.get("mortgage_insurance_deductible", False)
        ),
        "standard_deduction": params.get("standard_deduction", default={}),
        "note": (
            "Mortgage interest is an itemised deduction, so it is worth nothing until "
            "your itemised total beats the standard deduction shown here."
        ),
    }


@router.get("/payment-options")
def payment_reference() -> dict[str, Any]:
    return payments()


@router.get("/irs")
def irs_resources(state_code: str = Query(default="")) -> dict[str, Any]:
    """Official IRS links, grouped, plus the client's own state if they have one.

    Public and unauthenticated: none of it is about a person, and someone who
    cannot get past the sign-in screen may still need Where's My Refund.

    Every link opens in a new tab on the government's own domain. This system
    never proxies or frames a government site -- a client should always be able
    to see irs.gov in their own address bar.
    """
    config = resources()
    groups = [dict(group) for group in config.get("groups", [])]

    code = (state_code or "").strip().upper()
    if code:
        try:
            state = states().get(code)
        except LookupError:
            state = None
        if state and state.get("payment_url"):
            groups.append({
                "key": "state",
                "label": f"{state['name']}",
                "blurb": "Your state has its own return, deadlines and penalties.",
                "links": [{
                    "label": state.get("revenue_department", f"{state['name']} revenue"),
                    "url": state["payment_url"],
                    "note": ("A state payment is separate from the federal one. Paying "
                             "the IRS does not pay your state."),
                }],
            })
        elif state and state["type"] == "none":
            groups.append({
                "key": "state",
                "label": state["name"],
                "blurb": state.get("note", "No state income tax on wages."),
                "links": [],
            })

    return {
        "as_of": config.get("as_of", ""),
        "groups": groups,
        "note": (
            "These are the IRS's own pages, opened in a new tab. We never ask for "
            "your IRS credentials and never act on your IRS account."
        ),
    }
