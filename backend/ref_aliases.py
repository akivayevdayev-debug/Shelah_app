"""Spellings of a Mishneh Torah reference that Sefaria does not resolve.

The model (and most people) write the Rambam's code as "Hilchot Shabbat 2:1",
"Rambam, Hilkhot Shabbos 2" or "Laws of Shabbat 2"; Sefaria only knows the
English section titles ("Mishneh Torah, Sabbath 2:1") and its name lookup does
not bridge the transliteration, so such a cited source opened nothing in the
reader. ``canonical_mishneh_torah_ref`` rewrites the spelling to Sefaria's
title -- or returns ``None`` when the ref is not a Mishneh Torah one or its
section is not recognised, so an unknown name never lands on the wrong book.

The titles are the "Mishneh Torah, <section>" index entries on Sefaria.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

# Sefaria section title -> the transliterations it goes by. Matching ignores
# case, spaces, punctuation, doubled letters and the Ashkenazi "-os" ending
# (see _slug), so "Shabbat", "Shabbos" and "shabat" are all one spelling.
_SECTIONS: Dict[str, Tuple[str, ...]] = {
    "Foundations of the Torah": ("yesodei hatorah", "yesodey hatorah", "yesodot hatorah"),
    "Human Dispositions": ("deot", "deyot", "deos"),
    "Torah Study": ("talmud torah",),
    "Foreign Worship and Customs of the Nations": ("avodah zarah", "avodat kochavim", "avodat kokhavim", "avodat akum"),
    "Repentance": ("teshuvah", "teshuva"),
    "Reading the Shema": ("keriat shema", "kriat shema", "keriat shma"),
    "Prayer and the Priestly Blessing": ("tefillah", "tefillah uvirkat kohanim", "tefilah", "tefilla"),
    "Tefillin, Mezuzah and the Torah Scroll": ("tefillin", "tefillin mezuzah vesefer torah", "tefillin umezuzah vesefer torah"),
    "Fringes": ("tzitzit", "tzizit", "tzitzis"),
    "Blessings": ("berachot", "brachot", "berakhot", "berachos"),
    "Circumcision": ("milah", "mila"),
    "The Order of Prayer": ("seder hatefillah", "seder tefillah"),
    "Sabbath": ("shabbat", "shabbos", "shabbath", "shabat"),
    "Eruvin": ("eruvin", "eiruvin", "eruvim"),
    "Rest on the Tenth of Tishrei": ("shevitat asor", "shvitat asor", "shevitat haasor"),
    "Rest on a Holiday": ("yom tov", "shevitat yom tov", "shvitat yom tov"),
    "Leavened and Unleavened Bread": ("chametz umatzah", "chametz umatza", "chametz and matzah", "chametz umatsa"),
    "Shofar, Sukkah and Lulav": ("shofar sukkah vlulav", "shofar sukkah velulav", "shofar sukkah ulvav", "shofar sukkah vulav"),
    "Sheqel Dues": ("shekalim", "sheqalim", "shekelim"),
    "Sanctification of the New Month": ("kiddush hachodesh", "kidush hachodesh", "kiddush hakhodesh"),
    "Fasts": ("taaniyot", "taaniot", "taanit", "taanios"),
    "Scroll of Esther and Hanukkah": ("megillah vchanukkah", "megillah vechanukah", "megillah chanukah", "megillah vachanukkah"),
    "Marriage": ("ishut", "ishus"),
    "Divorce": ("gerushin", "gerushim"),
    "Levirate Marriage and Release": ("yibbum vchalitzah", "yibum vechalitzah", "yibum vchalitza"),
    "Virgin Maiden": ("naarah betulah", "naara betulah", "naarah besulah"),
    "Woman Suspected of Infidelity": ("sotah", "sota"),
    "Forbidden Intercourse": ("issurei biah", "issurei biyah", "issurey biah"),
    "Forbidden Foods": ("maachalot assurot", "maakhalot assurot", "maachalos assuros", "machalot assurot"),
    "Ritual Slaughter": ("shechitah", "shechita", "shehitah"),
    "Oaths": ("shevuot", "shevuos"),
    "Vows": ("nedarim",),
    "Nazariteship": ("nezirut", "nezirus"),
    "Appraisals and Devoted Property": ("arachin vcharamim", "arachin vecharamim", "arachin vcheramim"),
    "Diverse Species": ("kilayim", "kilaim"),
    "Gifts to the Poor": ("matnot aniyim", "matnot aniim", "matanot aniyim", "matnos aniyim"),
    "Heave Offerings": ("terumot", "terumos"),
    "Tithes": ("maaserot", "maasrot", "maaser", "maaseros"),
    "Second Tithes and Fourth Year's Fruit": ("maaser sheni", "maaser sheni vneta revai", "maaser sheni venetah revai"),
    "First Fruits and other Gifts to Priests Outside the Sanctuary": ("bikkurim", "bikurim"),
    "Sabbatical Year and the Jubilee": ("shemittah vyovel", "shemitah veyovel", "shmitah vyovel", "shemita veyovel"),
    "The Chosen Temple": ("beit habechirah", "beis habechirah", "beit habechira"),
    "Vessels of the Sanctuary and Those Who Serve Therein": ("keli hamikdash", "kelei hamikdash"),
    "Admission into the Sanctuary": ("biat hamikdash", "biat mikdash"),
    "Things Forbidden on the Altar": ("issurei hamizbeach", "issurei mizbeach", "issurei hamizbeyach"),
    "Sacrificial Procedure": ("maaseh hakorbanot", "maase hakorbanot", "maaseh hakorbonos"),
    "Daily Offerings and Additional Offerings": ("temidin umusafin", "temidim umusafim", "temidin umusafim"),
    "Sacrifices Rendered Unfit": ("pesulei hamukdashin", "pesulei hamukdashim"),
    "Service on the Day of Atonement": ("avodat yom hakippurim", "avodat yom hakippur", "avodas yom hakippurim"),
    "Trespass": ("meilah", "meila"),
    "Paschal Offering": ("korban pesach", "korbon pesach"),
    "Festival Offering": ("chagigah", "chagiga"),
    "Firstlings": ("bechorot", "bechoros"),
    "Offerings for Unintentional Transgressions": ("shegagot", "shegagos"),
    "Offerings for Those with Incomplete Atonement": ("mechusrei kapparah", "mechusrei kaparah"),
    "Substitution": ("temurah", "temura"),
    "Defilement by a Corpse": ("tumat met", "tumas meis", "tumat mes"),
    "Red Heifer": ("parah adumah", "para aduma"),
    "Defilement by Leprosy": ("tumat tzaraat", "tumas tzaraas"),
    "Those Who Defile Bed or Seat": ("metamei mishkav umoshav", "metamei mishkav umoshev"),
    "Other Sources of Defilement": ("shaar avot hatumah", "shear avos hatumah"),
    "Defilement of Foods": ("tumat ochlin", "tumat okhlin", "tumas ochlin"),
    "Vessels": ("kelim", "keilim", "kelim"),
    "Immersion Pools": ("mikvaot", "mikvaos", "mikvot"),
    "Damages to Property": ("nizkei mamon", "nizkei mammon", "nezikei mamon"),
    "Theft": ("genevah", "geneivah", "geneva"),
    "Robbery and Lost Property": ("gezelah vaavedah", "gezelah veavedah", "gezelah vaaveidah"),
    "One Who Injures a Person or Property": ("chovel umazik", "chovel umazzik", "chovel umezik"),
    "Murderer and the Preservation of Life": ("rotzeach ushmirat nefesh", "rotzeach", "rotseach"),
    "Sales": ("mechirah", "mekhira", "mechira"),
    "Ownerless Property and Gifts": ("zechiyah umatanah", "zekhiyah umatanah", "zechiya umatana"),
    "Neighbors": ("shecheinim", "shekheinim", "shechenim"),
    "Agents and Partners": ("sheluchin veshutafin", "shluchin veshutafin", "sheluchin vshutafin"),
    "Slaves": ("avadim", "avodim"),
    "Hiring": ("sechirut", "schirut", "sekhirut"),
    "Borrowing and Deposit": ("sheelah ufikadon", "shelah ufikadon", "shaalah ufikadon"),
    "Creditor and Debtor": ("malveh veloveh", "malveh vloveh"),
    "Plaintiff and Defendant": ("toen venitan", "toen vnitan"),
    "Inheritances": ("nachalot", "nahalot", "nachalos"),
    "The Sanhedrin and the Penalties within Their Jurisdiction": ("sanhedrin", "sanhedrin vhaonshin hamsurin lahem"),
    "Testimony": ("edut", "eidut", "edus"),
    "Rebels": ("mamrim",),
    "Mourning": ("avel", "evel"),
    "Kings and Wars": ("melachim umilchamot", "melachim umilchemot", "melachim"),
}

_FILLERS = re.compile(r"[^a-z]")


def _slug(value: str) -> str:
    """A section name reduced to what its spellings share: lower-case letters
    only, "kh" as "ch", "th" as "t", a closing "os" as "ot", repeated letters
    collapsed ("tefillah" and "tefilah"), and a closing "h" dropped."""
    text = _FILLERS.sub("", str(value or "").lower().replace("kh", "ch").replace("th", "t"))
    text = re.sub(r"os$", "ot", text)
    text = re.sub(r"(.)\1+", r"\1", text)
    return text[:-1] if text.endswith("h") and len(text) > 3 else text


_SLUGS: Dict[str, str] = {}
for _title, _spellings in _SECTIONS.items():
    _SLUGS[_slug(_title)] = _title
    for _spelling in _spellings:
        _SLUGS.setdefault(_slug(_spelling), _title)

# "[Mishneh Torah|Rambam|Maimonides][,] [Hilchot|Hilkhot|Laws of|Hil.] <section> [<chapter...>]"
_REF_RE = re.compile(
    r"""^\s*
    (?:(?:Mishneh\s+Torah|Mishne\s+Torah|Rambam|Maimonides)\b[\s,.:-]*)?
    (?:(?:Hilchot|Hilchos|Hilkhot|Hilkhoth|Hilcot|Hilchoth|Hil\.?|Laws?\s+of|Halachot|Halakhot)\s+)?
    (?P<name>[A-Za-z'’\- ]+?)
    (?P<rest>[\s,]+\d[\d\s:.,\-–]*)?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)
_PREFIX_RE = re.compile(
    r"^\s*(?:(?:Mishneh?\s+Torah|Rambam|Maimonides)\b|(?:Hilchot|Hilchos|Hilkhot|Hilkhoth|Hilcot|Hilchoth|Hil\.?|Laws?\s+of|Halachot|Halakhot)\s)",
    re.IGNORECASE,
)


def canonical_mishneh_torah_ref(ref: str) -> Optional[str]:
    """``"Mishneh Torah, <Sefaria section> <chapter...>"`` for a spelling of a
    Mishneh Torah reference, else ``None``.

    Only a ref that says it is the Rambam's -- it opens with "Mishneh Torah",
    "Rambam" or "Maimonides", or names "Hilchot"/"Laws of" -- is rewritten, so
    a bare "Shabbat 31b" (a Talmud page) is never mistaken for one.
    """
    text = str(ref or "").strip()
    if not text or not _PREFIX_RE.match(text):
        return None
    match = _REF_RE.match(text)
    if not match:
        return None
    title = _SLUGS.get(_slug(match.group("name")))
    if not title:
        return None
    rest = (match.group("rest") or "").strip(" ,")
    return f"Mishneh Torah, {title}" + (f" {rest}" if rest else "")
