"""Payment routes: how the refund arrives, or how the balance gets settled."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from taxos.api.deps import client_ip, current_taxpayer, get_db, verified_account
from taxos.auth import audit
from taxos.config import latest_year
from taxos.crypto import encrypt_field
from taxos.db.models import Estimate, PaymentPlan, Taxpayer
from taxos.engines.payments import build_payment_options, estimated_payments_for_next_year
from taxos.money import money

router = APIRouter(prefix="/api/payments", tags=["payments"])


class BankDetails(BaseModel):
    """Routing and account numbers, stored encrypted and never echoed back."""

    routing_number: str
    account_number: str
    account_type: str = Field(default="checking", description="checking or savings")


class ChoosePayment(BaseModel):
    estimate_id: int
    method: str
    jurisdiction: str = "federal"
    state_code: str = ""
    bank: BankDetails | None = None


def _valid_routing(number: str) -> bool:
    """ABA checksum. Catches a mistyped routing number before the IRS does."""
    digits = "".join(c for c in number if c.isdigit())
    if len(digits) != 9:
        return False
    weights = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    return sum(int(d) * w for d, w in zip(digits, weights)) % 10 == 0


@router.get("/options")
def options(
    balance: float = Query(description="Positive to pay, negative for a refund."),
    tax_year: int | None = Query(default=None),
    jurisdiction: str = Query(default="federal"),
    state_code: str = Query(default=""),
    can_pay_in_full: bool = Query(default=True),
    _account=Depends(verified_account),
) -> dict[str, Any]:
    """Price every route for a given balance."""
    direction, entries = build_payment_options(
        balance, year=tax_year or latest_year(), jurisdiction=jurisdiction,
        state_code=state_code, can_pay_in_full=can_pay_in_full,
    )
    return {
        "direction": direction,
        "balance": str(money(balance)),
        "options": [o.to_dict() for o in entries],
    }


@router.get("/next-year")
def next_year(
    current_year_tax: float = Query(...),
    current_year_agi: float = Query(...),
    expected_withholding: float = Query(default=0),
    tax_year: int | None = Query(default=None),
    _account=Depends(verified_account),
) -> dict[str, Any]:
    """The safe-harbour figure and quarterly schedule for next year."""
    return estimated_payments_for_next_year(
        current_year_tax=current_year_tax,
        current_year_agi=current_year_agi,
        expected_withholding=expected_withholding,
        year=tax_year or latest_year(),
    )


@router.post("/choose")
def choose(
    body: ChoosePayment, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record the client's choice, with bank details sealed if given."""
    estimate = session.get(Estimate, body.estimate_id)
    if estimate is None or estimate.taxpayer_id != taxpayer.id:
        raise HTTPException(status_code=404, detail="No such estimate on this account.")

    balance = (
        money(estimate.state_balance or 0) if body.jurisdiction == "state"
        else money(estimate.federal_balance or 0)
    )
    direction, entries = build_payment_options(
        balance, year=estimate.tax_year, jurisdiction=body.jurisdiction,
        state_code=body.state_code,
    )
    selected = next((o for o in entries if o.method == body.method), None)
    if selected is None:
        raise HTTPException(
            status_code=400,
            detail=f"{body.method!r} is not available for this balance. "
                   f"Available: {', '.join(o.method for o in entries if o.available)}",
        )
    if not selected.available:
        raise HTTPException(status_code=400, detail=selected.notes[0] if selected.notes
                            else "That option is not available for this balance.")

    plan = PaymentPlan(
        estimate_id=estimate.id,
        jurisdiction=body.jurisdiction,
        state_code=(body.state_code or "").upper(),
        direction=direction,
        method=selected.method,
        amount=selected.amount,
        instalments=selected.instalments,
        instalment_amount=selected.instalment_amount,
        first_due_on=date.fromisoformat(selected.first_due_on) if selected.first_due_on else None,
        setup_fee=selected.setup_fee,
        projected_interest=selected.interest,
        projected_penalty=selected.penalty,
        total_cost=selected.total_cost,
        schedule=selected.schedule,
        notes="\n".join(selected.notes),
    )

    if body.bank is not None:
        if not _valid_routing(body.bank.routing_number):
            raise HTTPException(
                status_code=400,
                detail="That routing number fails its checksum, so a digit is wrong. "
                       "A payment sent to a wrong but valid account is not recoverable.",
            )
        digits = "".join(c for c in body.bank.account_number if c.isdigit())
        if not 4 <= len(digits) <= 17:
            raise HTTPException(status_code=400, detail="That account number looks wrong.")
        context = f"estimate:{estimate.id}"
        plan.bank_routing_encrypted = encrypt_field(
            body.bank.routing_number, purpose="bank", context=context
        )
        plan.bank_account_encrypted = encrypt_field(
            body.bank.account_number, purpose="bank", context=context
        )
        plan.bank_account_last4 = digits[-4:]
        plan.bank_account_type = body.bank.account_type

    session.add(plan)
    session.flush()
    audit(session, "payment_chosen", account_id=account.id, actor=account.email,
          subject=f"payment_plan:{plan.id}", ip_address=client_ip(request),
          method=selected.method, jurisdiction=body.jurisdiction,
          bank_last4=plan.bank_account_last4)

    return {
        "id": plan.id,
        "direction": plan.direction,
        "method": plan.method,
        "label": selected.label,
        "amount": str(plan.amount),
        "instalments": plan.instalments,
        "instalment_amount": str(plan.instalment_amount),
        "total_cost": str(plan.total_cost),
        "first_due_on": selected.first_due_on,
        "bank_account": f"••••{plan.bank_account_last4}" if plan.bank_account_last4 else None,
        "schedule": plan.schedule,
        "notes": selected.notes,
    }
