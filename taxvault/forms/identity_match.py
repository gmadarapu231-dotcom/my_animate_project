"""Does this W-2 belong to the person who registered?

The IRS matches the name and Social Security number on a return against Social
Security Administration records, and a mismatch is the single most common cause
of an e-file rejection. Catching it here -- at the moment the W-2 goes in, when
it costs a minute to fix -- is worth far more than catching it after a filing
bounces.

Matching has to be tolerant without being useless. People are registered as
"Robert" and paid as "Bob"; a W-2 prints "MARIA J" where the account says
"Maria"; a married name appears on one and not the other; suffixes and
hyphenated surnames come and go. So the comparison is graded rather than
binary, and the *surname* carries the weight -- which is also how the SSA's own
match works, on the first four characters of the surname plus the SSN.

Nothing here blocks a filing. It produces a finding the client can act on,
because only they know whether "Reed" and "Reed-Santos" are the same person.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

#: Generational and professional suffixes, which are not part of the surname.
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v", "md", "phd", "dds", "esq", "cpa"}
#: Courtesy titles, likewise.
TITLES = {"mr", "mrs", "ms", "miss", "dr", "prof", "rev"}

#: The SSA matches on the first four characters of the surname.
SSA_SURNAME_PREFIX = 4


def normalise(text: str) -> str:
    """Strip accents, punctuation and case, so only the letters are compared."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"[^a-z\s]", " ", stripped.lower()).strip()


def tokens(name: str) -> list[str]:
    """The meaningful parts of a name: no titles, no suffixes, no initials."""
    parts = [p for p in normalise(name).split() if p]
    return [p for p in parts if p not in TITLES and p not in SUFFIXES and len(p) > 1]


def surname_parts(first: str, last: str, full: str = "") -> set[str]:
    """Every part of the surname.

    A surname is not always one word. "Reed" and "Reed-Santos" are the same
    person after a marriage, and reducing the second to its last token alone
    ("santos") makes them look like strangers. So the surname is a set, and two
    names match when their sets overlap.
    """
    if last and tokens(last):
        return set(tokens(last))
    parts = tokens(full or first)
    return {parts[-1]} if len(parts) > 1 else (set(parts) if parts else set())


def surname_of(first: str, last: str, full: str = "") -> str:
    """The single best surname token, for display and prefix comparison."""
    parts = surname_parts(first, last, full)
    if not parts:
        return ""
    if last and tokens(last):
        return tokens(last)[-1]
    return sorted(parts)[0]


def given_of(first: str, last: str, full: str = "") -> str:
    if first and tokens(first):
        return tokens(first)[0]
    parts = tokens(full or last)
    return parts[0] if parts else ""


@dataclass
class NameMatch:
    verdict: str            # exact | close | surname_only | mismatch | unknown
    severity: str           # ok | info | warning | error
    message: str
    registered: str = ""
    on_form: str = ""

    @property
    def matches(self) -> bool:
        return self.verdict in ("exact", "close", "surname_only")

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict, "severity": self.severity, "message": self.message,
            "registered": self.registered, "on_form": self.on_form, "matches": self.matches,
        }


def compare_names(
    *, registered_first: str = "", registered_last: str = "",
    form_first: str = "", form_last: str = "", form_full: str = "",
) -> NameMatch:
    """Grade how well the name on a W-2 matches the registered name."""
    registered_display = " ".join(p for p in (registered_first, registered_last) if p).strip()
    form_display = (form_full or " ".join(p for p in (form_first, form_last) if p)).strip()

    if not registered_display or not form_display:
        return NameMatch(
            verdict="unknown", severity="info",
            message=("The name on this W-2 could not be compared with the name on your "
                     "account, so check it yourself: the IRS matches the name and Social "
                     "Security number against Social Security records, and a mismatch is "
                     "rejected."),
            registered=registered_display, on_form=form_display,
        )

    reg_parts = surname_parts(registered_first, registered_last, registered_display)
    form_parts = surname_parts(form_first, form_last, form_display)
    reg_surname = surname_of(registered_first, registered_last, registered_display)
    form_surname = surname_of(form_first, form_last, form_display)
    reg_given = given_of(registered_first, registered_last, registered_display)
    form_given = given_of(form_first, form_last, form_display)

    # Overlapping surname parts is the real test: "Reed" and "Reed-Santos"
    # share one, "Reed" and "Santos-Rivera" share none.
    surname_same = bool(reg_parts & form_parts)
    # The SSA compares only the first four characters, so "Reed" and "Reede"
    # pass at their end while "Reed" and "Santos" do not.
    surname_prefix_same = bool(reg_surname) and bool(form_surname) and (
        reg_surname[:SSA_SURNAME_PREFIX] == form_surname[:SSA_SURNAME_PREFIX]
    )
    # A hyphenated or double surname on one side only is still the same person:
    # "Santos-Rivera" contains "Rivera".
    surname_contained = bool(reg_surname) and bool(form_surname) and (
        reg_surname in form_surname or form_surname in reg_surname
    )
    given_same = bool(reg_given) and reg_given == form_given

    if surname_same and given_same:
        return NameMatch(
            verdict="exact", severity="ok",
            message="The name on this W-2 matches your account.",
            registered=registered_display, on_form=form_display,
        )

    if (surname_same or surname_prefix_same or surname_contained) and given_same:
        return NameMatch(
            verdict="close", severity="info",
            message=(f"The W-2 is made out to {form_display}, and your account says "
                     f"{registered_display}. Close enough that the IRS match should "
                     "pass, but confirm the spelling is the one on your Social Security "
                     "card."),
            registered=registered_display, on_form=form_display,
        )

    if surname_same or surname_prefix_same or surname_contained:
        return NameMatch(
            verdict="surname_only", severity="warning",
            message=(f"The surname matches, but the W-2 says {form_display} where your "
                     f"account says {registered_display}. The IRS matches on the surname "
                     "and the Social Security number, so this will probably pass — check "
                     "it is the same person."),
            registered=registered_display, on_form=form_display,
        )

    return NameMatch(
        verdict="mismatch", severity="error",
        message=(f"This W-2 is made out to {form_display}, but the account is registered "
                 f"to {registered_display}. A return filed with a name that does not match "
                 "Social Security records is rejected by the IRS. Either this W-2 belongs "
                 "to someone else, or the name on the account needs correcting."),
        registered=registered_display, on_form=form_display,
    )


@dataclass
class SsnMatch:
    verdict: str            # exact | last4 | mismatch | unknown
    severity: str
    message: str

    @property
    def matches(self) -> bool:
        return self.verdict in ("exact", "last4")

    def to_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "severity": self.severity,
                "message": self.message, "matches": self.matches}


#: A W-2 prints the SSN in full or masked; both shapes are recognised.
#: `\b` cannot open this pattern: a masked number starts with "*", which is
#: not a word character, so there is no boundary before it to match.
SSN_ON_FORM = re.compile(
    r"(?<![\dA-Za-z])(\d{3}|[X*]{3})[-\s]?(\d{2}|[X*]{2})[-\s]?(\d{4})(?![\d])", re.I
)


def compare_ssn(form_ssn: str, *, registered_index: str, registered_last4: str) -> SsnMatch:
    """Check the number on the form against the one on the account.

    A full number is compared through the keyed blind index, so nothing is
    decrypted to do it. A masked number can only be compared on its last four,
    which is weaker but still catches the common case of a W-2 belonging to a
    spouse or a different household member.
    """
    from taxvault.crypto import normalise_ssn, ssn_index

    match = SSN_ON_FORM.search(form_ssn or "")
    if match is None:
        return SsnMatch("unknown", "info",
                        "No Social Security number could be read from this W-2.")

    area, group, serial = match.groups()
    masked = not area.isdigit() or not group.isdigit()

    if masked:
        if registered_last4 and serial == registered_last4:
            return SsnMatch("last4", "ok",
                            f"The W-2 is masked but ends in {serial}, which matches "
                            "the number on this account.")
        return SsnMatch(
            "mismatch", "error",
            f"This W-2 is for a Social Security number ending {serial}, but this "
            f"account's number ends {registered_last4 or '????'}. A W-2 belonging to "
            "someone else must not be filed on this return.")

    try:
        presented = normalise_ssn(f"{area}{group}{serial}")
    except ValueError:
        return SsnMatch("unknown", "info",
                        "The Social Security number on this W-2 could not be read.")

    if registered_index and ssn_index(presented) == registered_index:
        return SsnMatch("exact", "ok",
                        "The Social Security number on this W-2 matches this account.")
    return SsnMatch(
        "mismatch", "error",
        f"This W-2 is for a different Social Security number (ending {presented.last4}) "
        f"than the one on this account (ending {registered_last4 or '????'}). A W-2 "
        "belonging to someone else must not be filed on this return.")
