"""Reference data: the states, the years, and the limits.

Public and unauthenticated on purpose. None of it is about a person, and the
sign-in screen needs the state list before anyone has signed in.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from taxos.config import federal, latest_year, payments, states, supported_years
from taxos.money import money

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
