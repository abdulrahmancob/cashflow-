"""Form families: question anchors, option vocabularies and control geometry.

Every intake layout seen in the audit is described here. The reader never guesses an
option name from free text; it matches OCR lines against these phrases (R2, R3, R10, R11).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Option:
    code: str
    phrases: tuple[str, ...]
    writein: str = ""  # "" | "inline" (text after the label) | "below" (line under the label)
    column: int = 0  # printed column on two-column layouts
    control: bool = True  # False when the option is a write-in line with no box ("Other: ___" on the tiny form)


@dataclass(frozen=True)
class Family:
    id: str
    options: tuple[Option, ...]
    booking: tuple[Option, ...]
    control: str  # "box" | "circle" | "bullet"
    size: tuple[float, float]  # control side as multiples of the text height
    columns: int = 1
    zoom: float = 2.0


SOURCE_CODES = (
    "doctor",
    "google",
    "zocdoc",
    "social_media",
    "insurance",
    "friend_family",
    "event",
    "walk_in",
    "phone",
    "website",
    "direct_mail",
    "marketing_table",
    "clinic_staff",
    "flyer_doctor_office",
    "flyer_street",
    "lives_nearby",
    "other",
)

# Primary when more than one option is marked (R8).
PRIORITY = (
    "doctor",
    "insurance",
    "zocdoc",
    "google",
    "website",
    "social_media",
    "friend_family",
    "event",
    "flyer_doctor_office",
    "flyer_street",
    "marketing_table",
    "direct_mail",
    "clinic_staff",
    "walk_in",
    "lives_nearby",
    "phone",
    "other",
)

GROUPS = {
    "doctor": "Doctor referral",
    "google": "Google",
    "website": "Website",
    "zocdoc": "Zocdoc",
    "social_media": "Social media",
    "insurance": "Insurance",
    "friend_family": "Friend / family",
    "event": "Event / outreach",
    "flyer_doctor_office": "Flyers / marketing",
    "flyer_street": "Flyers / marketing",
    "marketing_table": "Flyers / marketing",
    "direct_mail": "Flyers / marketing",
    "clinic_staff": "Clinic staff",
    "walk_in": "Walk-in / nearby",
    "lives_nearby": "Walk-in / nearby",
    "phone": "Phone",
    "other": "Other",
    "multiple": "Multiple marks",
    "unmarked": "No mark",
    "no_question": "No question on form",
    "unreadable": "Unreadable",
}

QUESTION_RE = re.compile(
    r"how\s*did\s*you\s*hear\s*(?:about\s*us)?|hear\s*about\s*us|"
    r"c[oó]mo\s*nos\s*conoci[oó]|c[oó]mo\s*has\s*escuchado|escuchado\s*sobre\s*nosotros|"
    r"c[oó]mo\s*(?:se\s*)?enter[oó]|fuente\s*de\s*referencia|referred\s*by|referral\s*source|"
    r"how\s*did\s*you\s*learn\s*about",
    re.IGNORECASE,
)
QUESTION_PHRASES = (
    "how did you hear about us",
    "cómo nos conoció",
    "cómo has escuchado sobre nosotros",
)
BOOKING_RE = re.compile(
    r"how\s*did\s*you\s*book|c[oó]mo\s*reserv[oó]\s*su\s*cita|por\s*cu[aá]l\s*medio|programado\s*su\s*cita",
    re.IGNORECASE,
)
BOOKING_PHRASES = (
    "how did you book your appointment",
    "cómo reservó su cita",
    "por cuál medio has programado su cita",
)
SECTION_END_RE = re.compile(
    r"insurance\s*information|informaci[oó]n\s*del\s*seguro|patient\s*information|"
    r"informaci[oó]n\s*del\s*paciente|signature|firma\s*del|workers.?\s*compensation|"
    r"consentimiento|consent|financial\s*responsibility|responsabilidad\s*financiera|"
    r"medical\s*history|historial\s*m[eé]dico|appointment\s*policy|credit\s*card|"
    r"please\s*provide\s*insurance|policy\s*holder",
    re.IGNORECASE,
)
INSTRUCTION_RE = re.compile(
    r"please\s*check\s*what\s*applies|marque\s*lo\s*que\s*corresponda|check\s*all\s*that\s*apply",
    re.IGNORECASE,
)

_BOOK_EN = (
    Option("phone", ("phone call/ text", "phone call text", "phonecall text", "phone call", "phone / text", "phone text")),
    Option("zocdoc", ("zocdoc",)),
    Option("website", ("our website (www.ptofthecity.com)/google", "ourwebsite", "our website", "website / google", "website google")),
    Option("walk_in", ("walk-in", "walk in")),
)
_BOOK_ES_CHECK = (
    Option("phone", ("llamada telefónica", "llamada telefonica")),
    Option("zocdoc", ("zocdoc",)),
    Option("website", ("nuestro sitio web (www.ptofthecity.com)/ google", "nuestro sitio web")),
    Option("walk_in", ("entrando/pasando la oficina", "entrando pasando la oficina")),
)
_BOOK_ES_CIRCLE = (
    Option("phone", ("teléfono / mensaje de texto", "telefono mensaje de texto", "mensaje de texto")),
    Option("zocdoc", ("zocdoc",)),
    Option("website", ("sitio web / google", "sitio web google")),
    Option("walk_in", ("paciente sin cita previa", "sin cita previa")),
)

OLD_CHECKBOX = Family(
    id="old_checkbox",
    options=(
        Option("doctor", ("doctor's referral/recommendations", "doctors referral recommendations", "referral/recommendations", "doctor's referral"), "below"),
        Option("google", ("google",)),
        Option("zocdoc", ("zocdoc",)),
        Option("social_media", ("social media",)),
        Option("insurance", ("insurance recommendations",)),
        Option("direct_mail", ("direct mail",)),
        Option("friend_family", ("word of mouth",), "inline"),
        Option("marketing_table", ("marketing table",)),
        Option("event", ("event or community outreach",)),
        Option("clinic_staff", ("clinic staff",), "inline"),
        Option("flyer_doctor_office", ("from doctor office",)),
        Option("flyer_street", ("from street distribution",)),
        Option("other", ("other (please specify)", "other please specify", "please specify", "other"), "inline"),
    ),
    booking=_BOOK_EN,
    control="box",
    size=(0.55, 1.8),
)

OLD_BULLET = Family(
    id="old_bullet",
    options=(
        Option("doctor", ("doctor's referral/recommendations", "doctors referral recommendations", "referral/recommendations", "doctor's referral"), "below"),
        Option("google", ("google",)),
        Option("zocdoc", ("zocdoc",)),
        Option("social_media", ("social media",)),
        Option("insurance", ("insurance recommendations",)),
        Option("direct_mail", ("direct mail",)),
        Option("friend_family", ("word of mouth",), "inline"),
        Option("event", ("event or community outreach (flyers)", "event or community outreach")),
        Option("other", ("other (please specify)", "other please specify", "please specify", "other"), "inline"),
    ),
    booking=_BOOK_EN,
    control="bullet",
    size=(0.25, 0.85),
)

NEW_CIRCLE = Family(
    id="new_circle",
    options=(
        Option("doctor", ("doctor referral", "referral")),
        Option("google", ("google",), column=1),
        Option("zocdoc", ("zocdoc",)),
        Option("social_media", ("social media",), column=1),
        Option("insurance", ("insurance",)),
        Option("friend_family", ("word of mouth",), column=1),
        Option("event", ("event / outreach", "event outreach")),
        Option("other", ("other:", "other"), "inline"),
    ),
    booking=_BOOK_EN,
    control="circle",
    size=(0.55, 1.8),
    columns=2,
)

ES_CHECKBOX = Family(
    id="es_checkbox",
    options=(
        Option("doctor", ("remisión/recomendaciones del médico", "remision recomendaciones del medico", "recomendaciones del médico"), "below"),
        Option("google", ("google",)),
        Option("zocdoc", ("zocdoc",)),
        Option("social_media", ("redes sociales",)),
        Option("insurance", ("recomendaciones sobre seguros",)),
        Option("direct_mail", ("publicidad directa",)),
        Option("friend_family", ("de boca en boca",), "inline"),
        Option("marketing_table", ("tabla de marketing",)),
        Option("event", ("evento o difusión en la comunidad", "evento o difusion en la comunidad")),
        Option("clinic_staff", ("personal de la clínica", "personal de la clinica"), "inline"),
        Option("flyer_doctor_office", ("folletos de la oficina del doctor",)),
        Option("flyer_street", ("folletos de distribución en la calle", "folletos de distribucion en la calle")),
        Option("other", ("otros (especifique)", "otros especifique", "otros"), "inline"),
    ),
    booking=_BOOK_ES_CHECK,
    control="box",
    size=(0.55, 1.8),
)

ES_CIRCLE = Family(
    id="es_circle",
    options=(
        Option("doctor", ("una referencia médica", "una referencia medica", "referencia médica", "referencia")),
        Option("google", ("google",), column=1),
        Option("zocdoc", ("zocdoc",)),
        Option("social_media", ("redes sociales",), column=1),
        Option("insurance", ("seguro",)),
        Option("friend_family", ("recomendación personal", "recomendacion personal"), column=1),
        Option("event", ("evento / divulgación", "evento divulgacion")),
        Option("other", ("otro:", "otro"), "inline"),
    ),
    booking=_BOOK_ES_CIRCLE,
    control="circle",
    size=(0.55, 1.8),
    columns=2,
)

TINY = Family(
    id="tiny",
    options=(
        Option("doctor", ("doctor",)),
        Option("google", ("google",)),
        Option("social_media", ("social media",)),
        Option("zocdoc", ("zocdoc",)),
        Option("walk_in", ("walk-in", "walk in")),
        Option("event", ("flyers",)),
        Option("friend_family", ("friends/family", "friends family")),
        Option("lives_nearby", ("lives nearby",)),
        Option("other", ("other:", "other"), "inline", 0, False),
    ),
    booking=(),
    control="box",
    size=(0.6, 1.5),
    zoom=3.0,
)

GENERIC = Family(
    id="generic",
    options=tuple(
        dict.fromkeys(
            [*OLD_CHECKBOX.options, *NEW_CIRCLE.options, *ES_CHECKBOX.options, *ES_CIRCLE.options, *TINY.options],
        )
    ),
    booking=tuple(dict.fromkeys([*_BOOK_EN, *_BOOK_ES_CHECK, *_BOOK_ES_CIRCLE])),
    control="box",
    size=(0.4, 1.8),
    columns=2,
)

FAMILIES = {fam.id: fam for fam in (OLD_CHECKBOX, OLD_BULLET, NEW_CIRCLE, ES_CHECKBOX, ES_CIRCLE, TINY, GENERIC)}


def _low(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower())


def detect_family(block_text: str, page_text: str = "") -> Family:
    """Pick the layout from the words printed around the question."""
    block = _low(block_text)
    page = _low(page_text)
    both = block + " " + page
    if re.search(r"c[oó]mo has escuchado|escuchado sobre nosotros|una referencia m[eé]dica|sin cita previa", both):
        return ES_CIRCLE
    if re.search(r"c[oó]mo nos conoci|marque lo que corresponda|redes sociales|remisi[oó]n", both):
        return ES_CHECKBOX
    if re.search(r"friends\s*/\s*family|friends family|lives nearby", block):
        return TINY
    checkbox_words = re.search(r"marketing table|clinic\s*staff|from doctor office|street distribution", block)
    bullet_words = re.search(r"welcome to pt|\(flyers\)|working well|type doctor.s name\)", both)
    if re.search(r"please check what applies|typedoctor|type doctor|direct mail", both) or checkbox_words:
        if checkbox_words:
            return OLD_CHECKBOX
        if bullet_words:
            return OLD_BULLET
        return OLD_BULLET if re.search(r"\bo\s+(?:doctor|google|zocdoc)", block) else OLD_CHECKBOX
    if re.search(r"doctor referral|event\s*/\s*outreach|how did you find us|phone\s*/\s*text", both):
        return NEW_CIRCLE
    return GENERIC
