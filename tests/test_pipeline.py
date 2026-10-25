"""Regression tests for the deterministic pipeline, register lint and allowlist derivation.

Run: python -m unittest discover tests
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import normalize  # noqa: E402
import registerlib  # noqa: E402
import validate_register  # noqa: E402

NORMALIZE = ROOT / "scripts" / "normalize.py"


def norm(html: str, selector: str | None = None) -> str:
    return normalize.from_html(html.encode(), normalize.Selector(selector) if selector else None)


class NormalizeHtmlTests(unittest.TestCase):
    def test_deterministic(self):
        page = "<html><body><main><h1>DPA</h1><p>Clause 1.</p></main></body></html>"
        self.assertEqual(norm(page), norm(page))
        self.assertEqual(norm(page), "# DPA\nClause 1.\n")

    def test_void_and_optional_end_tags_do_not_swallow_content(self):
        # Each of these once left the drop counter open and discarded the rest
        # of the page, making every later amendment invisible.
        for chrome in (
            "<header><input type=search></header>",
            "<input type=checkbox>",
            "<video><source src=x.mp4></video>",
            "<select><option>A<option>B</select>",
        ):
            with self.subTest(chrome=chrome):
                out = norm(f"<body>{chrome}<p>Clause after chrome.</p></body>")
                self.assertIn("Clause after chrome.", out)

    def test_chrome_and_scripts_dropped(self):
        out = norm("<body><nav>Menu</nav><main><p>T</p><script nonce=a>x()</script></main><footer>(c)</footer></body>")
        self.assertEqual(out, "T\n")

    def test_header_inside_content_region_is_kept(self):
        # HTML5 <article><header> carries the title and effective date — the
        # evidence clock computation needs. Site-level header/footer still go.
        page = (
            "<body><header>Site menu</header><main><article>"
            "<header><h1>DPA</h1><p>Effective: January 1, 2025</p></header>"
            "<p>1. Scope.</p><footer>Version 3</footer></article></main>"
            "<footer>(c) 2025</footer></body>"
        )
        out = norm(page)
        self.assertEqual(out, "# DPA\nEffective: January 1, 2025\n\n1. Scope.\n\nVersion 3\n")

    def test_header_outside_region_dropped_and_drop_nesting_balanced(self):
        self.assertEqual(norm("<body><header><h1>Site</h1></header><div><p>Body</p></div></body>"), "Body\n")
        self.assertEqual(norm("<main><nav><header>x</header>menu</nav><p>Body</p></main>"), "Body\n")
        self.assertEqual(norm("<main><header><nav>menu</nav><h1>T</h1></header><p>Body</p></main>"), "# T\n\nBody\n")

    def test_selector_restricts_to_matching_element(self):
        page = (
            "<body><div class=cookie>We use cookies. Session 8f3a</div>"
            "<div id=terms class='legal wide'><h1>DPA</h1><p>Body</p></div>"
            "<div class=legal><p>Other</p></div><p>(c) 2025</p></body>"
        )
        self.assertEqual(norm(page, "#terms"), "# DPA\nBody\n")
        self.assertEqual(norm(page, "div#terms"), "# DPA\nBody\n")
        self.assertEqual(norm(page, "div.legal.wide"), "# DPA\nBody\n")
        self.assertEqual(norm(page, ".legal"), "# DPA\nBody\n\nOther\n")
        # Without a selector and without <main>, chrome churn lands in the hash.
        self.assertIn("cookies", norm(page))

    def test_selector_handles_nested_same_tag(self):
        page = "<body><div id=t><div><p>A</p></div><p>B</p></div><p>C</p></body>"
        self.assertEqual(norm(page, "div#t"), "A\n\nB\n")

    def test_selector_miss_is_an_error(self):
        with self.assertRaises(LookupError):
            norm("<main><p>x</p></main>", "#nope")
        for bad in ("", "div > p", "#a b", "[x]"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                normalize.Selector(bad)

    def test_invisible_and_unicode_spaces_canonicalized(self):
        out = norm("<main><p>Clause&nbsp;1​ and sub­processor x﻿</p></main>")
        self.assertEqual(out, "Clause 1 and subprocessor x\n")

    def test_charset_bom_meta_and_http_header(self):
        latin = "<html><head><meta charset=iso-8859-1></head><main><p>Caf\xe9 \xa7</p></main></html>".encode("latin-1")
        self.assertEqual(normalize.from_html(latin), "Café §\n")
        self.assertEqual(normalize.from_html(b"\xef\xbb\xbf<main><p>Caf\xc3\xa9</p></main>"), "Café\n")
        cp = "<main><p>Caf\xe9</p></main>".encode("cp1252")
        self.assertEqual(normalize.from_html(cp, charset="windows-1252"), "Café\n")
        self.assertEqual(normalize.sniff_charset(b"<p>x</p>", "no-such-charset"), "utf-8")

    def test_tables_and_lists(self):
        self.assertEqual(
            norm(
                "<main><table><tr><th>Entity</th><th>Location</th></tr><tr><td>AWS</td><td>US</td></tr></table></main>"
            ),
            "Entity | Location\n\nAWS | US\n",
        )
        self.assertEqual(norm("<main><ul><li><p>One<li><p>Two</ul></main>"), "- One\n- Two\n")


class NormalizeCliTests(unittest.TestCase):
    def run_cli(self, raw: bytes, *args: str, suffix: str = ".html") -> tuple[subprocess.CompletedProcess, dict | None]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        raw_path = tmp / f"raw{suffix}"
        raw_path.write_bytes(raw)
        meta_path = tmp / "meta.json"
        r = subprocess.run(
            [sys.executable, str(NORMALIZE), str(raw_path), "--meta", str(meta_path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
        return r, meta

    def test_empty_extraction_fails_loudly(self):
        r, meta = self.run_cli(b"<html><body><nav>only chrome</nav></body></html>")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")
        self.assertIsNone(meta)

    def test_selector_miss_fails_loudly(self):
        r, meta = self.run_cli(b"<main><p>x</p></main>", "--select", "#gone")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("restructured", r.stderr)
        self.assertIsNone(meta)

    def test_meta_sidecar_describes_pipeline(self):
        r, meta = self.run_cli(b"<html><body><main><p>Body</p></main></body></html>", "--select", "main")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "Body\n")
        assert meta is not None
        self.assertEqual(meta["normalizer_version"], normalize.NORMALIZER_VERSION)
        self.assertEqual(meta["source_format"], "html")
        self.assertEqual(meta["selector"], "main")
        self.assertEqual(meta["charset"], "utf-8")
        import hashlib

        self.assertEqual(meta["sha256"], hashlib.sha256(b"Body\n").hexdigest())

    def test_plain_text_passthrough(self):
        r, meta = self.run_cli(b"Terms\r\n\r\n\r\n  1. Clause  \n", suffix=".txt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "Terms\n\n1. Clause\n")
        # Detection is a heuristic for ad-hoc use; the run passes --format from the register.
        self.assertEqual(normalize.detect(b"Plain terms text.\n1. Clause"), "text")
        self.assertEqual(normalize.detect(b"<main><p>x</p></main>"), "html")
        self.assertEqual(normalize.detect(b"%PDF-1.7 ..."), "pdf")
        assert meta is not None
        self.assertEqual(meta["source_format"], "text")

    @unittest.skipUnless(normalize.pdf_version(), "pypdf not installed")
    def test_pdf_extraction_records_extractor_version(self):
        import io

        from pypdf import PdfWriter
        from pypdf.generic import DictionaryObject, NameObject, StreamObject

        w = PdfWriter()
        page = w.add_blank_page(width=200, height=200)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = StreamObject()
        content.set_data(b"BT /F1 24 Tf 20 100 Td (Hello Terms) Tj ET")
        page.replace_contents(content)
        buf = io.BytesIO()
        w.write(buf)

        r, meta = self.run_cli(buf.getvalue(), suffix=".pdf")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "Hello Terms\n")
        assert meta is not None
        self.assertEqual(meta["source_format"], "pdf")
        self.assertTrue(str(meta["extractor"]).startswith("pypdf "))


class ValidateRegisterTests(unittest.TestCase):
    def lint(self, register: str) -> tuple[list[str], list[str]]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        path = tmp / "watch-register.yaml"
        path.write_text(register)
        errors, warnings, _, _ = validate_register.lint(path)
        return errors, warnings

    def watch(self, wid="acme-dpa", url="https://acme.example/dpa", extra="", doc_extra="") -> str:
        return (
            "version: 1\ndefaults: {timezone: America/New_York}\n"
            f"watches:\n- id: {wid}\n  status: active\n  counterparty: Acme\n"
            f"  document: {{title: T, type: dpa, url: '{url}'{doc_extra}}}\n{extra}"
        )

    def assertErrors(self, register: str, *needles: str):
        errors, _ = self.lint(register)
        self.assertTrue(errors, "expected errors")
        for n in needles:
            self.assertTrue(any(n in e for e in errors), f"{n!r} not in {errors}")

    def test_shipped_register_is_valid(self):
        errors, warnings, total, active = validate_register.lint()
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual((total, active), (2, 0))

    def test_cli_entry_point(self):
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "validate_register.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("0 errors", r.stdout)

    def test_valid_watch_with_query_string(self):
        errors, _ = self.lint(self.watch(url="https://acme.example/dpa?v=2&lang=en"))
        self.assertEqual(errors, [])

    def test_path_traversal_id_rejected(self):
        self.assertErrors(self.watch(wid="'../../.claude'"), "slug")

    def test_shell_metacharacters_and_non_hosts_in_url_rejected(self):
        for url in (
            "https://acme.example/x$(id)",
            "https://acme.example/x`id`",
            "https://acme.example/a b",
            "http://acme.example/dpa",
            "https://10.0.0.1/dpa",
            "https://localhost/dpa",
        ):
            with self.subTest(url=url):
                self.assertErrors(self.watch(url=url), "document.url")

    def test_unknown_keys_are_errors(self):
        self.assertErrors(
            self.watch(extra="  rights:\n    amendment_notice: {window_day: 30, runs_from: notice, method: email}\n"),
            "unknown key(s) ['window_day']",
            "window_days",
        )
        self.assertErrors(
            self.watch(extra="  incorporated_by:\n  - {agreement: A, our_side: customer, renewal_dat: 2026-01-01}\n"),
            "renewal_dat",
        )
        self.assertErrors(self.watch(doc_extra=", selectr: main"), "selectr")
        self.assertErrors(self.watch(extra="  note: x\n"), "unknown key(s) ['note']")

    def test_dates_format_selector_timezone_version(self):
        # Unquoted, YAML itself rejects the impossible date; quoted, the field check does.
        self.assertErrors(
            self.watch(extra="  incorporated_by:\n  - {agreement: A, our_side: customer, executed: 2024-13-40}\n"),
            "cannot parse",
        )
        self.assertErrors(
            self.watch(extra="  incorporated_by:\n  - {agreement: A, our_side: customer, executed: '2024-13-40'}\n"),
            "executed",
        )
        self.assertErrors(
            self.watch(extra="  incorporated_by:\n  - {agreement: A, our_side: customer, executed: 'Nov 2 2024'}\n"),
            "executed",
        )
        errors, _ = self.lint(
            self.watch(
                extra="  incorporated_by:\n  - {agreement: A, our_side: customer, executed: 2024-11-02, owner: o}\n"
            )
        )
        self.assertEqual(errors, [])
        self.assertErrors(self.watch(doc_extra=", format: docx"), "document.format")
        self.assertErrors(self.watch(doc_extra=", selector: 'div > p'"), "document.selector")
        self.assertErrors(self.watch().replace("America/New_York", "Mars/Olympus"), "timezone")
        self.assertErrors(self.watch().replace("version: 1", "version: 2"), "version")
        self.assertErrors(
            self.watch(
                extra="  rights:\n    r: {window_days: true, runs_from: notice, method: email, required: yes}\n"
            ),
            "window_days",
        )

    def test_selector_hint_without_selector_warns(self):
        _, warnings = self.lint(self.watch(doc_extra=", selector_hint: 'the body'"))
        self.assertTrue(any("document.selector" in w for w in warnings))

    def test_findings_carry_their_watch_index(self):
        # A watch called `defaults` must not be able to claim `defaults.timezone` as its own error.
        reg = (
            "version: 1\ndefaults: {timezone: Mars/Olympus}\nwatches:\n"
            "- id: defaults\n  status: active\n  counterparty: Acme\n"
            "  document: {title: T, type: dpa, url: 'https://acme.example/dpa'}\n"
            "- id: defaults\n  status: active\n  counterparty: Acme\n"
            "  document: {title: T, type: dpa, url: 'https://acme.example/dpa2', format: docx}\n"
            "- 42\n"
        )
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))
        (tmp / "r.yaml").write_text(reg)
        report = validate_register.lint_report(tmp / "r.yaml")
        by_watch = {}
        for e in report.errors:
            by_watch.setdefault(e.watch, []).append(e.message)
        self.assertEqual(by_watch[None], ["defaults.timezone: 'Mars/Olympus' is not an IANA timezone"])
        self.assertEqual(by_watch[0], ["defaults: duplicate id (also watches[1])"])
        self.assertEqual(
            by_watch[1],
            ["defaults: duplicate id (also watches[0])", "defaults: document.format must be one of ['html', 'pdf']"],
        )
        self.assertEqual(by_watch[2], ["watches[2]: must be a mapping"])
        self.assertEqual([str(e) for e in report.register_errors()], by_watch[None])
        self.assertEqual([str(e) for e in report.watch_errors(2)], by_watch[2])
        self.assertEqual((report.watch_count, report.active_count), (3, 2))
        # The string view the CLI prints is the same findings, flattened.
        errors, _, total, active = validate_register.lint(tmp / "r.yaml")
        self.assertEqual(errors, [e.message for e in report.errors])
        self.assertEqual((total, active), (3, 2))

    def test_malformed_shapes_error_instead_of_crashing(self):
        for reg in (
            "",
            "watches: [oops]",
            "watches: [\n",
            "a: 2024-13-40",
            "watches: {a: b}",
            "defaults: 3",
            self.watch(extra="  rights: [a]\n"),
            self.watch(extra="  incorporated_by: {a: b}\n"),
        ):
            with self.subTest(reg=reg):
                errors, _ = self.lint(reg)
                self.assertTrue(errors)


class RegisterHostsTests(unittest.TestCase):
    def reg(self, *entries: tuple[str, str, str | None]) -> dict:
        return {
            "watches": [
                {"id": f"w{i}", "status": status, "document": {"url": url, "change_log_url": cl}}
                for i, (status, url, cl) in enumerate(entries)
            ]
        }

    def test_active_and_paused_hosts_only_with_www_parent(self):
        hosts = registerlib.register_hosts(
            self.reg(
                ("active", "https://www.AcmeCloud.example/legal/dpa", "https://trust.acmecloud.example/changes"),
                ("paused", "https://other.example/x", None),
                ("retired", "https://gone.example/x", None),
                ("example", "https://www.acmecloud.example/legal/subprocessors", None),
            )
        )
        self.assertEqual(
            hosts, ["acmecloud.example", "other.example", "trust.acmecloud.example", "www.acmecloud.example"]
        )

    def test_unallowlistable_hosts_skipped(self):
        hosts = registerlib.register_hosts(
            self.reg(
                ("active", "https://10.0.0.1/x", None),
                ("active", "https://localhost/x", None),
                ("active", "https://[::1]/x", None),
                ("active", "", None),
            )
        )
        self.assertEqual(hosts, [])

    def test_cap_enforced(self):
        entries = [("active", f"https://h{i}.example/x", None) for i in range(registerlib.MAX_ALLOWLIST_HOSTS + 1)]
        with self.assertRaises(ValueError):
            registerlib.register_hosts(self.reg(*entries))

    def test_shipped_register_round_trips(self):
        reg = registerlib.load_register()
        self.assertEqual(reg["version"], 1)
        self.assertEqual(registerlib.register_hosts(reg), [])  # examples only
        self.assertEqual(yaml.safe_load((ROOT / "register" / "watch-state.json").read_text()), {})


class PinConsistencyTests(unittest.TestCase):
    """The run skill installs pypdf by exact version inside the sandbox, and the
    tests here exercise the version pinned in requirements-dev.txt. Dependabot
    only ever bumps the latter, so the two drift apart silently unless a test
    holds them together: a mismatch means production snapshots are built with
    an extractor CI never ran."""

    def test_skill_pypdf_pin_matches_dev_requirements(self):
        import re

        dev = (ROOT / "requirements-dev.txt").read_text()
        skill = (ROOT / ".claude" / "skills" / "amendment-monitor-run" / "SKILL.md").read_text()
        dev_pin = re.search(r"^pypdf==(\S+)$", dev, re.M)
        skill_pins = set(re.findall(r"pip install pypdf==(\S+?)`", skill))
        self.assertIsNotNone(dev_pin, "requirements-dev.txt must pin pypdf==X.Y.Z")
        self.assertTrue(skill_pins, "SKILL.md must install pypdf by exact version")
        self.assertEqual(
            skill_pins, {dev_pin.group(1)}, "bump the pypdf pin in requirements-dev.txt and SKILL.md together"
        )


if __name__ == "__main__":
    unittest.main()
