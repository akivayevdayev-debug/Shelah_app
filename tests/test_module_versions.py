"""backend/module_versions.py: content-hashed ES module URLs and the shell's
import map (audit L-10)."""

import json
import os
import re

import pytest

from backend import module_versions


@pytest.fixture
def js_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(module_versions, "JS_DIR", tmp_path)
    module_versions._digest.cache_clear()
    yield tmp_path
    module_versions._digest.cache_clear()


def _write(path, text, bump_mtime=False):
    path.write_text(text)
    if bump_mtime:
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


class TestImportMap:
    def test_maps_each_es_module_to_its_hashed_url(self, js_dir):
        _write(js_dir / "router.js", "export function readRoute() {}\n")
        _write(js_dir / "main.js", 'import { readRoute } from "./router.js";\n')
        imports = module_versions.import_map()["imports"]
        assert set(imports) == {"/static/js/main.js", "/static/js/router.js"}
        assert re.fullmatch(r"/static/js/router\.js\?v=[0-9a-f]{12}", imports["/static/js/router.js"])

    def test_classic_scripts_and_stray_copies_are_left_out(self, js_dir):
        _write(js_dir / "router.js", "export const x = 1;\n")
        _write(js_dir / "topbar_shared.js", "// import nothing\n  import('x');\nmodule.exports = {};\n")
        _write(js_dir / "router 2.js", "export const x = 1;\n")
        _write(js_dir / "notes.txt", "export const x = 1;\n")
        assert list(module_versions.import_map()["imports"]) == ["/static/js/router.js"]

    def test_a_changed_module_gets_a_new_url_and_an_unchanged_one_keeps_its_own(self, js_dir):
        _write(js_dir / "router.js", "export const v = 1;\n")
        _write(js_dir / "state.js", "export const s = 1;\n")
        before = module_versions.import_map()["imports"]
        _write(js_dir / "router.js", "export const v = 2;\n", bump_mtime=True)
        after = module_versions.import_map()["imports"]
        assert after["/static/js/router.js"] != before["/static/js/router.js"]
        assert after["/static/js/state.js"] == before["/static/js/state.js"]

    def test_the_same_bytes_hash_the_same_whatever_the_mtime(self, js_dir):
        _write(js_dir / "router.js", "export const v = 1;\n")
        before = module_versions.module_url("router.js")
        _write(js_dir / "router.js", "export const v = 1;\n", bump_mtime=True)
        assert module_versions.module_url("router.js") == before

    def test_an_unreadable_module_is_skipped(self, js_dir, monkeypatch):
        _write(js_dir / "router.js", "export const v = 1;\n")
        _write(js_dir / "state.js", "export const s = 1;\n")
        real_stat = type(js_dir).stat

        def flaky_stat(self, *args, **kwargs):
            if self.name == "state.js":
                raise OSError("gone")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(type(js_dir), "stat", flaky_stat)
        assert list(module_versions.import_map()["imports"]) == ["/static/js/router.js"]

    def test_module_url_falls_back_to_the_plain_url(self, js_dir):
        assert module_versions.module_url("missing.js") == "/static/js/missing.js"


class TestShell:
    def test_the_real_modules_are_all_mapped(self):
        imports = module_versions.import_map()["imports"]
        for name in ("main.js", "router.js", "state.js", "conversation-ui.js", "motion.js"):
            assert f"/static/js/{name}" in imports, name

    def test_the_shell_carries_the_map_before_any_module_and_hashed_entry_tags(self, test_client):
        html = test_client.get("/").get_data(as_text=True)
        found = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
        assert found, "import map missing"
        imports = json.loads(found.group(1))["imports"]
        assert imports == module_versions.import_map()["imports"]
        assert found.start() < html.index('<script type="module"')
        entry = re.search(r'<script type="module" src="(/static/js/main\.js[^"]*)"', html).group(1)
        assert entry == imports["/static/js/main.js"], "an entry and its import share one URL"


@pytest.fixture
def static_dir(tmp_path, monkeypatch):
    (tmp_path / "css").mkdir()
    monkeypatch.setattr(module_versions, "STATIC_DIR", tmp_path)
    module_versions._file_digest.cache_clear()
    yield tmp_path
    module_versions._file_digest.cache_clear()


class TestAssetUrl:
    """asset_url: /static/<name>?v=<hash of its bytes>, for the templates'
    stylesheets and classic scripts."""

    def test_the_url_carries_a_hash_of_the_bytes(self, static_dir):
        _write(static_dir / "css" / "reader.css", "a { color: red }\n")
        url = module_versions.asset_url("css/reader.css")
        assert re.fullmatch(r"/static/css/reader\.css\?v=[0-9a-f]{12}", url)
        assert module_versions.is_content_hash(url.split("v=")[1])

    def test_changed_bytes_are_a_new_url_and_the_same_bytes_are_the_same_url(self, static_dir):
        css = static_dir / "css" / "reader.css"
        _write(css, "a { color: red }\n")
        first = module_versions.asset_url("css/reader.css")
        _write(css, "a { color: red }\n", bump_mtime=True)
        assert module_versions.asset_url("css/reader.css") == first
        _write(css, "a { color: blue }\n", bump_mtime=True)
        assert module_versions.asset_url("css/reader.css") != first

    def test_a_file_that_is_not_there_gets_its_plain_url(self, static_dir):
        assert module_versions.asset_url("css/missing.css") == "/static/css/missing.css"

    def test_a_name_that_escapes_static_gets_its_plain_url_and_no_digest(self, static_dir):
        (static_dir.parent / "secret.txt").write_text("nope")
        assert "?v=" not in module_versions.asset_url("../secret.txt")

    def test_only_a_twelve_digit_hash_counts_as_a_content_hash(self):
        assert module_versions.is_content_hash("0123456789ab")
        for version in (None, "", "20261009-cv16", "0123456789abc", "0123456789AB", "0123456789a"):
            assert not module_versions.is_content_hash(version), version


class TestShellAssetTags:
    def test_every_stylesheet_and_script_tag_carries_a_content_hash(self, test_client):
        html = test_client.get("/").get_data(as_text=True)
        tags = re.findall(r'(?:href|src)="(/static/(?:css/|js/)?[\w.-]+\.(?:css|js))(\?v=[^"]*)?"', html)
        assert tags
        unversioned = [path for path, query in tags if not query]
        hand_versioned = [path + q for path, q in tags if q and not module_versions.is_content_hash(q[3:])]
        # Every tag asks asset_url: none keeps a version someone has to bump.
        assert hand_versioned == []
        assert unversioned == []

    def test_a_legal_page_and_the_404_use_hashed_urls_too(self, test_client):
        for path in ("/terms", "/about", "/definitely-not-a-page"):
            html = test_client.get(path).get_data(as_text=True)
            assert re.search(r'/static/css/tokens\.css\?v=[0-9a-f]{12}"', html), path
            assert not re.search(r'/static/[\w./-]+\?v=\d{8}', html), path
