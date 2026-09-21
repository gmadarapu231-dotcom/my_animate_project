"""Reference data: the states, the years, and the limits.

Public and unauthenticated on purpose. None of it is about a person, and the
sign-in screen needs the state list before anyone has signed in.
"""

from __future__ import annotations

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
from taxvault.money import money

router = APIRouter(prefix="/api/reference", tags=["reference"])


@router.get("/years")
def years() -> dict[str, Any]:
    current = latest_year()
    params = federal(current)
    return {
        "supported": supported_years(),
        "current": current,
        "due_date": params.due_date,
        "extended_due_date": params.extended_due_date,
        "source": params.source,
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
        "brackets": {
            status: [
                {"from": str(b.floor), "to": None if b.ceiling is None else str(b.ceiling),
                 "rate": str(b.rate)}
                for b in params.brackets(status)
            ]
            for status in params.statuses
        },
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
