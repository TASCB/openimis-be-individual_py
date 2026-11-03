"""Relationship-to-head codebook shared by every ingestion path (api_etl, legacy_individual,
individual imports).

Each code gives a ``label`` (the questionnaire's wording) and a ``role`` (the stored
GroupIndividual.Role).
"""
from collections import namedtuple
from typing import Optional, Tuple

OTHER_RELATIVE = "OTHER RELATIVE"

Variant = namedtuple("Variant", "label role")
Relationship = namedtuple("Relationship", "male female unknown")


def _ungendered(label, role):
    """Same relationship whatever the member's sex."""
    variant = Variant(label, role)
    return Relationship(variant, variant, variant)


def _gendered(male_label, female_label, male_role, female_role):
    """Both sexes valid, different answers. Unknown sex is not guessed."""
    return Relationship(
        Variant(male_label, male_role),
        Variant(female_label, female_role),
        Variant(OTHER_RELATIVE, OTHER_RELATIVE),
    )


def _female_only(label, role):
    """Only a woman holds it: unknown sex resolves as female; a male widens to OTHER RELATIVE."""
    female = Variant(label, role)
    return Relationship(Variant(label, OTHER_RELATIVE), female, female)


CODEBOOK = {
    "1":  _ungendered("HEAD", "HEAD"),
    "2":  _ungendered("SPOUSE", "SPOUSE"),
    "3":  _gendered("SON", "DAUGHTER", "SON", "DAUGHTER"),
    "4":  _gendered("SON IN LAW", "DAUGHTER IN LAW", "SON IN LAW", "DAUGHTER IN LAW"),
    "5":  _gendered("GRANDSON", "GRANDDAUGHTER", "GRANDSON", "GRANDDAUGHTER"),
    "6":  _gendered("BIOLOGICAL FATHER", "BIOLOGICAL MOTHER", "FATHER", "MOTHER"),
    "7":  _gendered("FATHER IN LAW", "MOTHER IN LAW", "FATHER IN LAW", "MOTHER IN LAW"),
    "8":  _gendered("BROTHER", "SISTER", "BROTHER", "SISTER"),
    "9":  _ungendered(OTHER_RELATIVE, OTHER_RELATIVE),
    "10": _ungendered("STEP CHILD", "STEP CHILD"),
    "11": _female_only("CO-WIFE", "CO-WIFE"),
    "12": _gendered(
        "BROTHER IN LAW", "SISTER IN LAW", "BROTHER IN LAW", "SISTER IN LAW"
    ),
    "13": _ungendered("MSAIDIZI WA NYUMBANI", "HOUSE HELP"),
}

# A code outside the codebook. The questionnaire calls this NOT DEFINED; the
# enum has no such member, so it widens like any unclassified relative.
UNDEFINED = _ungendered("NOT DEFINED", OTHER_RELATIVE)

# Reverse lookup: a stored role that only one sex can hold implies that sex.
# Lets the backfill recover sex for rows imported before it was persisted.
# OTHER RELATIVE is reachable by both sexes, so it never implies one.
GENDER_BY_ROLE = {}
for _entry in CODEBOOK.values():
    if _entry.male.role != _entry.female.role:
        if _entry.male.role != OTHER_RELATIVE:
            GENDER_BY_ROLE.setdefault(_entry.male.role, "M")
        if _entry.female.role != OTHER_RELATIVE:
            GENDER_BY_ROLE.setdefault(_entry.female.role, "F")


def normalize_gender(gender) -> Optional[str]:
    g = str(gender or "").strip().upper()
    if g in ("M", "MALE"):
        return "M"
    if g in ("F", "FEMALE"):
        return "F"
    return None


def describe_relationship(relationship_code, gender) -> Tuple[Optional[str], Optional[str]]:
    """Resolve a code plus sex to ``(label, role)``; ``(None, None)`` if no code."""
    code = str(relationship_code or "").strip()
    if not code:
        return None, None

    entry = CODEBOOK.get(code, UNDEFINED)
    normalized = normalize_gender(gender)
    if normalized == "M":
        return entry.male
    if normalized == "F":
        return entry.female
    return entry.unknown


def role_for_relationship(relationship_code, gender) -> Optional[str]:
    """The GroupIndividual.Role value for a code plus sex."""
    return describe_relationship(relationship_code, gender)[1]


def label_for_relationship(relationship_code, gender) -> Optional[str]:
    """The questionnaire's own description for a code plus sex."""
    return describe_relationship(relationship_code, gender)[0]
