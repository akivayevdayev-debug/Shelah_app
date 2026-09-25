/**
 * Hebrew reference helpers for the reader: Hebrew numerals (gematria) and the
 * verse-accurate weekly Torah portion for a Chumash reference.
 *
 * The parashah boundaries are Sefaria's own (the "Parasha" alt structure of
 * each book's index, /api/v2/raw/index/<Book> -> alt_structs.Parasha.nodes[].
 * wholeRef), so "Numbers 4:21" is Naso, not Bamidbar. English names keep the
 * spellings templates/index.html's PARASHA_HE_MAP already uses.
 *
 * Loaded as a plain classic <script> before index.html's inline script, which
 * reads window.ShelahHebrewRef; node tests require() it.
 */
(function (root) {
    'use strict';

    const ONES = ['', 'א', 'ב', 'ג', 'ד', 'ה', 'ו', 'ז', 'ח', 'ט'];
    const TENS = ['', 'י', 'כ', 'ל', 'מ', 'נ', 'ס', 'ע', 'פ', 'צ'];
    const HUNDREDS = ['', 'ק', 'ר', 'ש', 'ת'];
    const GERESH = '׳';
    const GERSHAYIM = '״';

    // 1 -> "א׳", 15 -> "ט״ו", 21 -> "כ״א", 345 -> "שמ״ה". Numbers of 1000+
    // keep only the last three digits (5787 -> "תשפ״ז"), the usual way of
    // writing a Hebrew year. Anything that isn't a positive integer comes back
    // unchanged as a string.
    function hebrewNumeral(value, { punctuate = true } = {}) {
        const number = Number(value);
        if (!Number.isInteger(number) || number <= 0) return String(value ?? '');
        let n = number % 1000 || number;
        if (n >= 1000) return String(value);
        let letters = '';
        while (n >= 400) {
            letters += 'ת';
            n -= 400;
        }
        if (n >= 100) {
            letters += HUNDREDS[Math.floor(n / 100)];
            n %= 100;
        }
        // 15 and 16 are written ט״ו / ט״ז so they don't spell the divine name.
        if (n === 15) letters += 'טו';
        else if (n === 16) letters += 'טז';
        else letters += TENS[Math.floor(n / 10)] + ONES[n % 10];
        if (!punctuate) return letters;
        return letters.length === 1
            ? letters + GERESH
            : `${letters.slice(0, -1)}${GERSHAYIM}${letters.slice(-1)}`;
    }

    // [book, startChapter, startVerse, English name, Hebrew name], in order.
    const PARASHOT = [
        ['Genesis', 1, 1, 'Bereshit', 'בראשית'],
        ['Genesis', 6, 9, 'Noach', 'נח'],
        ['Genesis', 12, 1, 'Lech-Lecha', 'לך לך'],
        ['Genesis', 18, 1, 'Vayera', 'וירא'],
        ['Genesis', 23, 1, 'Chayei Sara', 'חיי שרה'],
        ['Genesis', 25, 19, 'Toldot', 'תולדות'],
        ['Genesis', 28, 10, 'Vayetze', 'ויצא'],
        ['Genesis', 32, 4, 'Vayishlach', 'וישלח'],
        ['Genesis', 37, 1, 'Vayeshev', 'וישב'],
        ['Genesis', 41, 1, 'Miketz', 'מקץ'],
        ['Genesis', 44, 18, 'Vayigash', 'ויגש'],
        ['Genesis', 47, 28, 'Vayechi', 'ויחי'],
        ['Exodus', 1, 1, 'Shemot', 'שמות'],
        ['Exodus', 6, 2, 'Vaera', 'וארא'],
        ['Exodus', 10, 1, 'Bo', 'בא'],
        ['Exodus', 13, 17, 'Beshalach', 'בשלח'],
        ['Exodus', 18, 1, 'Yitro', 'יתרו'],
        ['Exodus', 21, 1, 'Mishpatim', 'משפטים'],
        ['Exodus', 25, 1, 'Terumah', 'תרומה'],
        ['Exodus', 27, 20, 'Tetzaveh', 'תצוה'],
        ['Exodus', 30, 11, 'Ki Tisa', 'כי תשא'],
        ['Exodus', 35, 1, 'Vayakhel', 'ויקהל'],
        ['Exodus', 38, 21, 'Pekudei', 'פקודי'],
        ['Leviticus', 1, 1, 'Vayikra', 'ויקרא'],
        ['Leviticus', 6, 1, 'Tzav', 'צו'],
        ['Leviticus', 9, 1, 'Shemini', 'שמיני'],
        ['Leviticus', 12, 1, 'Tazria', 'תזריע'],
        ['Leviticus', 14, 1, 'Metzora', 'מצורע'],
        ['Leviticus', 16, 1, 'Achrei Mot', 'אחרי מות'],
        ['Leviticus', 19, 1, 'Kedoshim', 'קדושים'],
        ['Leviticus', 21, 1, 'Emor', 'אמור'],
        ['Leviticus', 25, 1, 'Behar', 'בהר'],
        ['Leviticus', 26, 3, 'Bechukotai', 'בחוקותי'],
        ['Numbers', 1, 1, 'Bamidbar', 'במדבר'],
        ['Numbers', 4, 21, 'Naso', 'נשא'],
        ['Numbers', 8, 1, 'Behaalotecha', 'בהעלותך'],
        ['Numbers', 13, 1, 'Shelach', 'שלח'],
        ['Numbers', 16, 1, 'Korach', 'קרח'],
        ['Numbers', 19, 1, 'Chukat', 'חקת'],
        ['Numbers', 22, 2, 'Balak', 'בלק'],
        ['Numbers', 25, 10, 'Pinchas', 'פינחס'],
        ['Numbers', 30, 2, 'Matot', 'מטות'],
        ['Numbers', 33, 1, 'Masei', 'מסעי'],
        ['Deuteronomy', 1, 1, 'Devarim', 'דברים'],
        ['Deuteronomy', 3, 23, 'Vaetchanan', 'ואתחנן'],
        ['Deuteronomy', 7, 12, 'Eikev', 'עקב'],
        ['Deuteronomy', 11, 26, 'Reeh', 'ראה'],
        ['Deuteronomy', 16, 18, 'Shoftim', 'שופטים'],
        ['Deuteronomy', 21, 10, 'Ki Teitzei', 'כי תצא'],
        ['Deuteronomy', 26, 1, 'Ki Tavo', 'כי תבוא'],
        ['Deuteronomy', 29, 9, 'Nitzavim', 'נצבים'],
        ['Deuteronomy', 31, 1, 'Vayelech', 'וילך'],
        ['Deuteronomy', 32, 1, 'Haazinu', 'האזינו'],
        ['Deuteronomy', 33, 1, 'Vezot Haberakhah', 'וזאת הברכה'],
    ].map(([book, chapter, verse, name, he]) => Object.freeze({ book, chapter, verse, name, he }));

    const TORAH_BOOKS = ['Genesis', 'Exodus', 'Leviticus', 'Numbers', 'Deuteronomy'];

    // {book, chapter, verse|null} for "Numbers 5", "Numbers 5:3", "Numbers.5.3",
    // "Numbers 5:3-7" or "Numbers 4:21-7:89" (the start of a range); null for
    // anything outside the five books.
    function parseTorahRef(ref) {
        const text = String(ref || '').replace(/[._]/g, ' ').replace(/\s+/g, ' ').trim();
        const match = text.match(/^(Genesis|Exodus|Leviticus|Numbers|Deuteronomy) (\d+)(?:[ :](\d+))?(?:-.*)?$/i);
        if (!match) return null;
        const book = TORAH_BOOKS.find((name) => name.toLowerCase() === match[1].toLowerCase());
        return { book, chapter: Number(match[2]), verse: match[3] ? Number(match[3]) : null };
    }

    function startsAtOrBefore(entry, chapter, verse) {
        return entry.chapter < chapter || (entry.chapter === chapter && entry.verse <= verse);
    }

    // The portion a single verse belongs to.
    function parashaAt(book, chapter, verse = 1) {
        let found = null;
        for (const entry of PARASHOT) {
            if (entry.book !== book) continue;
            if (!startsAtOrBefore(entry, chapter, verse)) break;
            found = entry;
        }
        return found;
    }

    // Every portion with at least one verse in the chapter: the one verse 1
    // sits in, plus any that begin later in the same chapter.
    function parashotInChapter(book, chapter) {
        const first = parashaAt(book, chapter, 1);
        const later = PARASHOT.filter((e) => e.book === book && e.chapter === chapter && e.verse > 1);
        return [first, ...later].filter(Boolean);
    }

    function parashotForRef(ref) {
        const parsed = parseTorahRef(ref);
        if (!parsed) return [];
        if (parsed.verse) {
            const entry = parashaAt(parsed.book, parsed.chapter, parsed.verse);
            return entry ? [entry] : [];
        }
        return parashotInChapter(parsed.book, parsed.chapter);
    }

    // "Parashat Naso" / "פרשת נשא"; a chapter split between two portions
    // names both ("Parashot Bamidbar–Naso"). Empty outside the Chumash.
    function parashaLabel(ref, lang = 'en') {
        const entries = parashotForRef(ref);
        if (!entries.length) return '';
        const hebrew = lang === 'he';
        const names = entries.map((e) => (hebrew ? e.he : e.name)).join('–');
        if (entries.length > 1) return hebrew ? `פרשות ${names}` : `Parashot ${names}`;
        return hebrew ? `פרשת ${names}` : `Parashat ${names}`;
    }

    // "4:21" / "ד׳:כ״א": where a portion starts, for the chapter-grid chips.
    function parashaStartLabel(entry, lang = 'en') {
        if (!entry) return '';
        if (lang !== 'he') return `${entry.chapter}:${entry.verse}`;
        return `${hebrewNumeral(entry.chapter)}:${hebrewNumeral(entry.verse)}`;
    }

    // {Genesis: [{chapter, verse, name}], ...}, the shape index.html's
    // TORAH_PARASHA_BY_BOOK has always had, now with the starting verse.
    function parashotByBook() {
        const byBook = {};
        for (const entry of PARASHOT) {
            (byBook[entry.book] = byBook[entry.book] || []).push({
                chapter: entry.chapter, verse: entry.verse, name: entry.name,
            });
        }
        return byBook;
    }

    const api = {
        hebrewNumeral,
        parseTorahRef,
        parashaAt,
        parashotInChapter,
        parashotForRef,
        parashaLabel,
        parashaStartLabel,
        parashotByBook,
        PARASHOT,
    };

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = api;
    } else {
        root.ShelahHebrewRef = api;
    }
})(typeof window !== 'undefined' ? window : globalThis);
