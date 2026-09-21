"""The preparation fee, the client's money, and paying the tax on their behalf.

Two things live here and the separation between them is the point.

A **fee** is what the client owes the practice for the work. It is the
practice's revenue, quoted up front and itemised, and it is collected by Zelle
or bank transfer like any other invoice.

A **remittance** is the client's own tax money, passing through on its way to
the IRS. It is held in trust, it is only moved on a recorded instruction from
the client, and it is never touched for anything else.

Every route here is scoped to the signed-in account. The batch routes, which
act across clients, are restricted to a preparer.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from taxvault.api.deps import client_ip, current_taxpayer, get_db, verified_account
from taxvault.auth import audit
from taxvault.config import latest_year
from taxvault.db.models import (
    Account,
    Estimate,
    FeeQuoteRecord,
    RemittanceAuthorization,
    RemittanceBatch,
    Taxpayer,
)
from taxvault.engines import fees as fee_engine
from taxvault.engines.remittance import (
    BUCKET_FEE,
    BUCKET_TAX,
    RemittanceError,
    authorize,
    balance,
    batch_file,
    compliance_checklist,
    mark_submitted,
    prepare_batch,
    ready_to_remit,
    record_funds,
    revoke,
    statement,
)
from taxvault.money import money

router = APIRouter(prefix="/api/billing", tags=["billing"])


def preparer(account: Account = Depends(verified_account)) -> Account:
    """Routes that act across clients, not just the signed-in one."""
    if account.role not in ("preparer", "admin"):
        raise HTTPException(
            status_code=403,
            detail="Only a preparer can work across client accounts.",
        )
    return account


# ---------------------------------------------------------------------------
# the fee
# ---------------------------------------------------------------------------
class FeeRequest(BaseModel):
    estimate_id: int | None = None
    tax_year: int | None = None
    w2_count: int = 1
    prior_year_returns: int = 0
    planning_session: bool = False
    returning_client: bool = False
    filed_early: bool = False
    save: bool = True


@router.get("/price-list")
def price_list(_account=Depends(verified_account)) -> dict[str, Any]:
    """What the practice charges, before anyone's figures are involved."""
    from taxvault.config import fees as fee_config

    config = fee_config()
    return {
        "promise": config.get("promise", ""),
        "minimum": str(money(config.get("minimum", 0))),
        "maximum": str(money(config.get("maximum", 0))),
        "tiers": [
            {"key": key, "label": entry["label"], "base": str(money(entry["base"])),
             "when": entry.get("when", ""), "description": entry.get("description", "")}
            for key, entry in config.get("tiers", {}).items()
        ],
        "add_ons": [
            {"key": key, "label": entry["label"], "amount": str(money(entry["amount"])),
             "note": entry.get("note", "")}
            for key, entry in config.get("add_ons", {}).items()
        ],
        "collection": config.get("collection", {}),
    }


@router.post("/quote")
def quote(
    body: FeeRequest, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Price this engagement from the return itself, itemised.

    Reading the return rather than asking the client to describe it means the
    quote reflects the work that is actually there -- and the client can see
    every line of it.
    """
    year = body.tax_year or latest_year()

    estimate_row = None
    if body.estimate_id:
        estimate_row = session.get(Estimate, body.estimate_id)
        if estimate_row is None or estimate_row.taxpayer_id != taxpayer.id:
            raise HTTPException(status_code=404, detail="No such estimate on this account.")

    if estimate_row is not None:
        breakdown = estimate_row.breakdown or {}
        states = breakdown.get("states", [])
        result = fee_engine.quote(
            agi=estimate_row.federal_agi or 0,
            itemised=any(
                "Itemised" in (line.get("label") or "")
                for line in breakdown.get("federal_lines", [])
            ),
            w2_count=max(1, body.w2_count),
            state_count=max(1, len(states)),
            local_returns=sum(1 for s in states if money(s.get("local_tax", 0)) > 0),
            prior_year_returns=body.prior_year_returns,
            planning_session=body.planning_session,
            returning_client=body.returning_client,
            filed_early=body.filed_early,
        )
    else:
        result = fee_engine.quote(
            w2_count=max(1, body.w2_count),
            prior_year_returns=body.prior_year_returns,
            planning_session=body.planning_session,
            returning_client=body.returning_client,
            filed_early=body.filed_early,
        )

    payload = result.to_dict()
    if body.save:
        row = FeeQuoteRecord(
            taxpayer_id=taxpayer.id,
            estimate_id=estimate_row.id if estimate_row else None,
            tax_year=year, tier=result.tier, base=result.base,
            add_ons=result.add_ons, discount=result.discount, total=result.total,
            lines=[line.to_dict() for line in result.lines],
        )
        session.add(row)
        session.flush()
        payload["quote_id"] = row.id
        audit(session, "fee_quoted", account_id=account.id, actor=account.email,
              subject=f"fee_quote:{row.id}", ip_address=client_ip(request),
              total=str(result.total), tier=result.tier)
    return payload


class FeePaid(BaseModel):
    quote_id: int
    method: str = Field(default="zelle", description="zelle, ach_debit or card")
    amount: float
    reference: str = Field(default="", description="The Zelle or bank reference.")


@router.post("/fee/paid")
def fee_paid(
    body: FeePaid, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record a preparation fee received. The practice's own money."""
    row = session.get(FeeQuoteRecord, body.quote_id)
    if row is None or row.taxpayer_id != taxpayer.id:
        raise HTTPException(status_code=404, detail="No such quote on this account.")
    try:
        entry = record_funds(
            session, taxpayer, amount=body.amount, bucket=BUCKET_FEE,
            direction="received", method=body.method, reference=body.reference,
            tax_year=row.tax_year, note="Preparation fee",
        )
    except RemittanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row.paid_at = entry.at
    row.payment_method = body.method
    row.payment_reference = body.reference or None
    session.flush()

    audit(session, "fee_received", account_id=account.id, actor=account.email,
          subject=f"fee_quote:{row.id}", ip_address=client_ip(request),
          method=body.method, amount=str(entry.amount))
    return {
        "recorded": True, "amount": str(entry.amount), "method": body.method,
        "fees_received": str(balance(session, taxpayer.id, bucket=BUCKET_FEE)),
        "outstanding": str(money(row.total) - balance(session, taxpayer.id, bucket=BUCKET_FEE)),
        "note": "Recorded against your preparation fee. This is separate from your tax.",
    }


# ---------------------------------------------------------------------------
# money held for the client
# ---------------------------------------------------------------------------
class TaxFundsReceived(BaseModel):
    amount: float
    tax_year: int | None = None
    method: str = "zelle"
    reference: str = Field(default="", description="The Zelle or bank reference.")


@router.get("/ledger")
def ledger(
    _account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Both ledgers, so the client can see their money is kept apart."""
    return statement(session, taxpayer.id)


@router.post("/funds")
def funds_received(
    body: TaxFundsReceived, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record tax money received from a client, held in trust for them."""
    try:
        entry = record_funds(
            session, taxpayer, amount=body.amount, bucket=BUCKET_TAX,
            direction="received", method=body.method, reference=body.reference,
            tax_year=body.tax_year or latest_year(),
            note="Received for onward payment of tax",
        )
    except RemittanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    audit(session, "tax_funds_received", account_id=account.id, actor=account.email,
          subject=f"trust_entry:{entry.id}", ip_address=client_ip(request),
          method=body.method, amount=str(entry.amount))
    return {
        "recorded": True,
        "amount": str(entry.amount),
        "held_for_tax": str(entry.balance_after),
        "note": (
            "Held for you and kept separate from the preparation fee. It goes to the "
            "tax authority only once you authorise it, and comes back to you if you "
            "withdraw the instruction."
        ),
    }


# ---------------------------------------------------------------------------
# authorising the payment
# ---------------------------------------------------------------------------
class AuthorizeRequest(BaseModel):
    amount: float
    tax_year: int | None = None
    jurisdiction: str = "federal"
    state_code: str = ""
    estimate_id: int | None = None
    #: The client has to say yes explicitly. A default of true would make the
    #: whole record worthless.
    agreed: bool = False


@router.get("/authorizations")
def list_authorizations(
    _account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    rows = session.scalars(
        select(RemittanceAuthorization)
        .where(RemittanceAuthorization.taxpayer_id == taxpayer.id)
        .order_by(RemittanceAuthorization.id.desc())
    ).all()
    return {
        "held_for_tax": str(balance(session, taxpayer.id, bucket=BUCKET_TAX)),
        "authorizations": [
            {
                "id": row.id, "tax_year": row.tax_year, "jurisdiction": row.jurisdiction,
                "state_code": row.state_code, "amount": str(row.amount),
                "status": row.status, "statement": row.statement,
                "authorized_at": row.authorized_at.isoformat() if row.authorized_at else None,
                "remitted_at": row.remitted_at.isoformat() if row.remitted_at else None,
                "confirmation_number": row.confirmation_number,
                "can_withdraw": row.status == "authorized",
            }
            for row in rows
        ],
    }


@router.post("/authorize")
def authorize_payment(
    body: AuthorizeRequest, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """The client's instruction to pay their tax on their behalf."""
    if not body.agreed:
        raise HTTPException(
            status_code=400,
            detail="Read the authorisation and agree to it before it can be recorded.",
        )
    try:
        authorization = authorize(
            session, taxpayer, amount=body.amount,
            tax_year=body.tax_year or latest_year(),
            jurisdiction=body.jurisdiction, state_code=body.state_code,
            estimate_id=body.estimate_id, ip_address=client_ip(request),
            practice_name="TaxVault",
        )
    except RemittanceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    held = balance(session, taxpayer.id, bucket=BUCKET_TAX)
    audit(session, "remittance_authorized", account_id=account.id, actor=account.email,
          subject=f"authorization:{authorization.id}", ip_address=client_ip(request),
          amount=str(authorization.amount), tax_year=authorization.tax_year,
          jurisdiction=authorization.jurisdiction)

    shortfall = money(authorization.amount) - held
    return {
        "id": authorization.id,
        "amount": str(authorization.amount),
        "tax_year": authorization.tax_year,
        "jurisdiction": authorization.jurisdiction,
        "statement": authorization.statement,
        "held_for_tax": str(held),
        "funded": shortfall <= 0,
        "shortfall": str(max(shortfall, money(0))),
        "note": (
            "Authorised and fully funded. It goes in the next batch."
            if shortfall <= 0 else
            f"Authorised, but {shortfall:,.2f} more is needed before it can be sent. "
            "Nothing is paid until the full amount is held."
        ),
    }


@router.post("/authorizations/{authorization_id}/withdraw")
def withdraw(
    authorization_id: int, request: Request,
    account=Depends(verified_account),
    taxpayer: Taxpayer = Depends(current_taxpayer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Withdraw an instruction, while the money is still here."""
    authorization = session.get(RemittanceAuthorization, authorization_id)
    if authorization is None or authorization.taxpayer_id != taxpayer.id:
        raise HTTPException(status_code=404, detail="No such authorisation on this account.")
    try:
        revoke(session, authorization)
    except RemittanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit(session, "remittance_withdrawn", account_id=account.id, actor=account.email,
          subject=f"authorization:{authorization.id}", ip_address=client_ip(request))
    return {
        "id": authorization.id, "status": authorization.status,
        "note": ("Withdrawn. Nothing will be sent, and any money held for this "
                 "payment is returned to you."),
    }


# ---------------------------------------------------------------------------
# the practice's side
# ---------------------------------------------------------------------------
@router.get("/remittance/pending")
def pending(
    jurisdiction: str = Query(default="federal"),
    state_code: str = Query(default=""),
    _preparer=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Everything authorised, and whether the money is actually there."""
    rows = ready_to_remit(session, jurisdiction=jurisdiction, state_code=state_code)
    return {
        "pending": [
            {
                "authorization_id": authorization.id,
                "name": taxpayer.display_name,
                "ssn_last4": taxpayer.ssn_last4,
                "tax_year": authorization.tax_year,
                "amount": str(authorization.amount),
                "held": str(held),
                "funded": held >= money(authorization.amount),
            }
            for authorization, taxpayer, held in rows
        ],
        "checklist": compliance_checklist(),
    }


@router.post("/remittance/batch")
def create_batch(
    request: Request,
    jurisdiction: str = Query(default="federal"),
    state_code: str = Query(default=""),
    tax_year: int | None = Query(default=None),
    account=Depends(preparer),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Assemble the next batch. Nothing is sent; this produces the list."""
    result = prepare_batch(
        session, jurisdiction=jurisdiction, state_code=state_code, tax_year=tax_year,
    )
    audit(session, "remittance_batch_prepared", account_id=account.id,
          actor=account.email, subject=f"batch:{result.batch.reference}",
          ip_address=client_ip(request), item_count=result.batch.item_count,
          total=str(result.batch.total), skipped=len(result.skipped))
    return {
        **result.to_dict(),
        "note": (
            "Prepared, not sent. Download the file, submit it through EFTPS Batch "
            "Provider, then record the result here."
        ),
    }


@router.get("/remittance/batch/{reference}/file")
def download_batch(
    reference: str, _preparer=Depends(preparer), session: Session = Depends(get_db),
) -> Response:
    """The batch as CSV. Carries the last four of each number, never the number."""
    batch = session.scalars(
        select(RemittanceBatch).where(RemittanceBatch.reference == reference)
    ).first()
    if batch is None:
        raise HTTPException(status_code=404, detail="No such batch.")
    return Response(
        content=batch_file(session, batch),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{reference}.csv"',
            "Cache-Control": "no-store",
        },
    )


class BatchSubmitted(BaseModel):
    confirmations: dict[int, str] = Field(
        default_factory=dict,
        description="Confirmation number per authorisation id, as EFTPS returns them.",
    )


@router.post("/remittance/batch/{reference}/submitted")
def batch_submitted(
    reference: str, body: BatchSubmitted, request: Request,
    account=Depends(preparer), session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Record that the batch was submitted, with its confirmations."""
    batch = session.scalars(
        select(RemittanceBatch).where(RemittanceBatch.reference == reference)
    ).first()
    if batch is None:
        raise HTTPException(status_code=404, detail="No such batch.")
    try:
        mark_submitted(session, batch, confirmations=body.confirmations)
    except RemittanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    audit(session, "remittance_batch_submitted", account_id=account.id,
          actor=account.email, subject=f"batch:{batch.reference}",
          ip_address=client_ip(request), item_count=batch.item_count,
          confirmations=len(body.confirmations))
    return {
        "reference": batch.reference, "status": batch.status,
        "item_count": batch.item_count, "total": str(batch.total),
        "note": (
            "Recorded. Keep the EFTPS acknowledgement: it is the practice's evidence "
            "that each client's tax was paid."
        ),
    }


@router.get("/remittance/checklist")
def checklist(_preparer=Depends(preparer)) -> dict[str, Any]:
    """What has to be true before a first live remittance."""
    return {
        "checklist": compliance_checklist(),
        "note": (
            "None of this can be enforced in code. A practice that discovers these "
            "after taking client money discovers them expensively."
        ),
    }
