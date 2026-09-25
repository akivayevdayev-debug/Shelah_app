/**
 * Hebrew names for the community-customs data (customs/*.json), which is
 * written in English: the categories, topics, source authorities and origin
 * lines the customs page and modal show as titles. Rulings and practice lists
 * are prose and stay as written.
 *
 * Loaded as a plain classic <script> before index.html's inline script, which
 * reads window.ShelahCommunityLabels; node tests require() it.
 */
(function (root) {
    'use strict';

    const HE = {
        // Categories
        calendar: 'לוח השנה',
        community: 'קהילה',
        education: 'חינוך',
        'family law': 'דיני משפחה',
        general: 'כללי',
        kashrut: 'כשרות',
        lifecycle: 'מעגל החיים',
        mourning: 'אבלות',
        pesach: 'פסח',
        prayer: 'תפילה',
        shabbat: 'שבת',
        tefillin: 'תפילין',
        tzitzit: 'ציצית',

        // Topics
        avelut: 'אבלות',
        'avelut and hashkava': 'אבלות והשכבה',
        'baked goods and chametz concerns': 'מאפים וחשש חמץ',
        bakkashot: 'בקשות',
        'bishul akum and food preparation': 'בישול עכו״ם והכנת מזון',
        'brit milah': 'ברית מילה',
        'candle lighting': 'הדלקת נרות',
        'carrying and eruv': 'הוצאה ועירוב',
        'chametz removal': 'ביעור חמץ',
        chanukah: 'חנוכה',
        'chanukah and purim': 'חנוכה ופורים',
        'community authority': 'סמכות הקהילה',
        'community gathering': 'התכנסות הקהילה',
        'cooking and carrying': 'בישול והוצאה',
        'cooking and food preparation': 'בישול והכנת מזון',
        'cooking and reheating': 'בישול וחימום',
        'cooking and warming': 'בישול וחימום',
        'cooking and warming food': 'בישול וחימום מזון',
        'dietary separation': 'הפרדה במזון',
        'family purity': 'טהרת המשפחה',
        'food customs': 'מנהגי מאכל',
        'food purity and utensils': 'כשרות מזון וכלים',
        'funeral customs': 'מנהגי הלוויה',
        "ge'ez and hebrew usage": 'שימוש בגעז ובעברית',
        'head coverings and synagogue decorum': 'כיסוי ראש וכבוד בית הכנסת',
        'hebrew pronunciation': 'הגיית העברית',
        'heritage preservation': 'שימור המורשת',
        'high holidays': 'הימים הנוראים',
        'interactions with majority society': 'יחסים עם החברה הסובבת',
        'kabbalat shabbat': 'קבלת שבת',
        'kaddish, kedushah, and communal service': 'קדיש, קדושה ותפילה בציבור',
        kitniyot: 'קטניות',
        'kitniyot and grain customs': 'קטניות ומנהגי דגנים',
        'kitniyot and grains': 'קטניות ודגנים',
        'kitniyot and regional foods': 'קטניות ומאכלים מקומיים',
        'kitniyot and rice': 'קטניות ואורז',
        'kitniyot on pesach': 'קטניות בפסח',
        'marriage and celebrations': 'נישואין ושמחות',
        'marriage and family formation': 'נישואין והקמת משפחה',
        'marriage and henna customs': 'נישואין ומנהגי חינה',
        'marriage and ketubah': 'נישואין וכתובה',
        'marriage and wedding customs': 'נישואין ומנהגי חתונה',
        'matzah and festival foods': 'מצה ומאכלי החג',
        'meals and hospitality': 'סעודות והכנסת אורחים',
        'meat and dairy': 'בשר וחלב',
        'meat and dairy separation': 'הפרדת בשר וחלב',
        'meat and slaughter': 'בשר ושחיטה',
        'milk, meat, and local foodways': 'חלב, בשר ומנהגי מאכל מקומיים',
        'niddah and family purity': 'נידה וטהרת המשפחה',
        'niddah and mikveh': 'נידה ומקווה',
        'nusach and liturgy': 'נוסח וסדר התפילה',
        'nusach and pronunciation': 'נוסח והגייה',
        'nusach ashkenaz and nusach sfard': 'נוסח אשכנז ונוסח ספרד',
        'passover observance': 'שמירת הפסח',
        'pesach customs': 'מנהגי פסח',
        'piyutim and poetry': 'פיוטים ושירה',
        purim: 'פורים',
        'romaniote nusach': 'נוסח רומניוטי',
        'seder and haggadah': 'הסדר וההגדה',
        'seder customs': 'מנהגי הסדר',
        'seder practice': 'עריכת הסדר',
        'selichot and penitential prayers': 'סליחות ותפילות תשובה',
        'sephardi influence': 'השפעה ספרדית',
        'shabbat meals': 'סעודות שבת',
        'shabbat observance': 'שמירת שבת',
        shechita: 'שחיטה',
        'shechita and meat inspection': 'שחיטה ובדיקת בשר',
        'shechita and meat supervision': 'שחיטה והשגחה על הבשר',
        'shechita and supervision': 'שחיטה והשגחה',
        'stam yeinam and wine': 'סתם יינם ויין',
        'study and synagogue life': 'לימוד וחיי בית הכנסת',
        'subgroups and minhag': 'עדות משנה ומנהג',
        'synagogue and communal structure': 'בית הכנסת ומבנה הקהילה',
        'tallit and fringes': 'טלית וציציות',
        'tefillah order': 'סדר התפילה',
        'tefillin practice': 'הנחת תפילין',
        "tishah b'av and fasts": 'תשעה באב והתעניות',
        'torah and language transmission': 'מסירת התורה והלשון',
        'torah and scriptural focus': 'תורה ומקרא',
        'torah reading': 'קריאת התורה',
        'torah reading and seating': 'קריאת התורה וסדרי הישיבה',
        'torah reading cantillation': 'טעמי קריאת התורה',
        'torah study': 'תלמוד תורה',
        'torah study and transmission': 'לימוד התורה ומסירתה',
        'torah transmission': 'מסירת התורה',
        'toshavim and megorashim': 'תושבים ומגורשים',
        wine: 'יין',
        'wine and beverages': 'יין ומשקאות',
        'wine and ritual beverages': 'יין ומשקאות של מצווה',
        'wine and ritual use': 'יין לדבר מצווה',

        // Source authorities
        'aruch hashulchan': 'ערוך השולחן',
        'babylonian rabbinic tradition': 'מסורת חכמי בבל',
        'beit yosef': 'בית יוסף',
        'beta israel communal tradition': 'מסורת קהילת ביתא ישראל',
        'biblical tradition as preserved in beta israel practice': 'מסורת המקרא כפי שנשמרה בביתא ישראל',
        'chayei adam': 'חיי אדם',
        'later rabbinic guidance': 'הוראת האחרונים',
        'mishnah berurah': 'משנה ברורה',
        'mishneh torah': 'משנה תורה',
        'moroccan rabbinic literature': 'ספרות חכמי מרוקו',
        rambam: 'רמב״ם',
        'rav chaim palachi': 'הרב חיים פלאג׳י',
        'rav refael saban': 'הרב רפאל סבאן',
        rema: 'רמ״א',
        rif: 'רי״ף',
        'romaniote communal tradition': 'מסורת הקהילה הרומניוטית',
        'sephardi communal psak': 'פסיקת הקהילות הספרדיות',
        'shulchan aruch': 'שולחן ערוך',
        'syrian communal tradition': 'מסורת הקהילה הסורית',
        torah: 'תורה',
        'yemenite communal tradition': 'מסורת קהילות תימן',
        'yemenite legal tradition': 'מסורת ההלכה של יהדות תימן',
        'local bukharan custom': 'מנהג בוכרה המקומי',
        'local bukharan tradition': 'מסורת בוכרה המקומית',
        'local georgian jewish tradition': 'מסורת יהודי גאורגיה',
        'local mountain jewish tradition': 'מסורת יהודי ההרים',
        'local persian jewish tradition': 'מסורת יהודי פרס',
        'local persian communal custom': 'מנהג קהילות פרס',
        'local community tradition': 'מסורת הקהילה המקומית',

        // Origin lines
        'historic jewish communities of iraq, especially baghdad, basra, and mosul':
            'הקהילות היהודיות ההיסטוריות של עיראק, ובמיוחד בגדד, בצרה ומוסול',
        'ancient jewish communities of syria, especially aleppo and damascus':
            'הקהילות היהודיות העתיקות של סוריה, ובמיוחד חלב ודמשק',
        'historic jewish communities of morocco and the wider maghreb':
            'הקהילות היהודיות ההיסטוריות של מרוקו ושל המגרב כולו',
        'jewish communities of medieval and early modern central and eastern europe':
            'קהילות היהודים במרכז אירופה ובמזרחה בימי הביניים ובראשית העת החדשה',
        'historic jewish communities of georgia, especially tbilisi, kutaisi, and surrounding regions':
            'הקהילות היהודיות ההיסטוריות של גאורגיה, ובמיוחד טביליסי, כותאיסי וסביבותיהן',
        'ancient jewish communities of the caucasus region, especially dagestan and azerbaijan':
            'הקהילות היהודיות העתיקות של הקווקז, ובמיוחד דאגסטן ואזרבייג׳ן',
        'ottoman empire jewish communities, especially turkish-based sephardi settlements after 1492':
            'קהילות היהודים באימפריה העות׳מאנית, ובמיוחד היישובים הספרדיים בטורקיה אחרי 1492',
        'ethiopia, especially the communities historically associated with beta israel':
            'אתיופיה, ובמיוחד הקהילות המזוהות היסטורית עם ביתא ישראל',
        'historic jewish communities of iran, especially tehran, isfahan, shiraz, and other persian centers':
            'הקהילות היהודיות ההיסטוריות של איראן, ובמיוחד טהרן, אספהאן, שיראז ומרכזים פרסיים נוספים',
        'historic jewish communities of greece and the broader byzantine/greek world':
            'הקהילות היהודיות ההיסטוריות של יוון ושל העולם הביזנטי־יווני',
        'ancient jewish communities in yemen and the arabian peninsula':
            'הקהילות היהודיות העתיקות של תימן וחצי האי ערב',
        'iberian jewish civilization before and after the 1492 expulsion from spain':
            'יהדות חצי האי האיברי לפני גירוש ספרד (1492) ואחריו',
        'central asia, especially the historic jewish communities of bukhara and surrounding regions':
            'מרכז אסיה, ובמיוחד הקהילות היהודיות ההיסטוריות של בוכרה וסביבתה',
    };

    // Case, spacing and the data's odd hyphen (U+2011 in "Turkish‑based") don't
    // change the lookup.
    function keyOf(text) {
        return String(text ?? '').replace(/[‐-―]/g, '-').replace(/\s+/g, ' ').trim().toLowerCase();
    }

    // The Hebrew name, or null when the text isn't one we know.
    function he(text) {
        return Object.prototype.hasOwnProperty.call(HE, keyOf(text)) ? HE[keyOf(text)] : null;
    }

    // A label in the UI language: Hebrew when known, else the English as given.
    function label(text, lang) {
        return (lang === 'he' && he(text)) || String(text ?? '');
    }

    // A comma-separated list of sources, each named in the UI language.
    function sourceList(text, lang) {
        const raw = String(text ?? '').trim();
        if (lang !== 'he' || !raw) return raw;
        if (he(raw)) return he(raw);
        return raw.split(/\s*,\s*/).filter(Boolean).map((part) => label(part, lang)).join(', ');
    }

    const api = { he, label, sourceList };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    } else {
        root.ShelahCommunityLabels = api;
    }
})(typeof window !== 'undefined' ? window : globalThis);
