"""Country packs and taxonomy are data, not code."""

from __future__ import annotations

import pytest

from careeros.config import CountryRegistry, countries, taxonomy


def test_both_first_class_countries_load():
    codes = countries().codes
    assert "US" in codes and "IN" in codes


def test_country_detected_from_free_text():
    assert countries().detect("Bengaluru, Karnataka, India").code == "IN"
    assert countries().detect("Austin, Texas, United States").code == "US"


def test_unknown_country_raises_with_guidance():
    with pytest.raises(KeyError, match="config/countries"):
        countries().require("Atlantis")


def test_a_new_country_needs_no_code_change(tmp_path):
    """The whole point of the country-pack design."""
    (tmp_path / "canada.yaml").write_text(
        """
country_code: CA
country_name: Canada
aliases: [canada, "ca"]
locale: {currency: CAD, currency_symbol: "C$", date_format: "%Y-%m-%d"}
work_authorization:
  statuses:
    - {id: citizen, label: Canadian Citizen, needs_sponsorship_now: false,
       needs_sponsorship_future: false, satisfies: [citizen, permanent, any_authorized], aliases: []}
    - {id: unknown, label: Unknown, needs_sponsorship_now: true,
       needs_sponsorship_future: true, satisfies: [], aliases: []}
  employment_types: [{id: full_time, label: Full-Time, contract: false}]
  jd_signals:
    sponsorship_unavailable:
      severity: blocking_if_needs_sponsorship
      patterns: ["must be legally entitled to work in canada"]
salary: {formats: [{id: annual, period: year, patterns: ["per year"]}]}
""",
        encoding="utf-8",
    )
    registry = CountryRegistry(tmp_path)
    assert registry.codes == ["CA"]
    assert registry.require("canada").currency == "CAD"


def test_skill_graph_has_no_dangling_edges():
    tax = taxonomy()
    ids = set(tax.skills)
    for node in tax.skills.values():
        for edge in (*node.related, *node.broader, *node.narrower):
            assert edge in ids, f"{node.id} points at unknown skill {edge}"


def test_graph_edges_are_symmetric_when_read():
    """YAML declares edges one way; the registry closes them both ways."""
    tax = taxonomy()
    assert "iam" in tax.neighbours("active_directory")["broader"]
    assert "active_directory" in tax.neighbours("iam")["narrower"]
