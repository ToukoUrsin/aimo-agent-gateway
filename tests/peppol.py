"""Validate real gateway orders against the published Peppol T01 artifacts.

The cache is deliberately outside the repository.  Every downloaded artifact is
content-addressed by the SHA-256 below; a missing file, a network failure, or a
changed upstream file is a hard test failure.  Run with ``AIMO_DATA=/tmp/aimo-peppol``.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import urllib.request
import zipfile
from pathlib import Path

from lxml import etree
from saxonche import PySaxonProcessor

from gateway import menu, peppol
from gateway.service import Gateway


# OASIS UBL 2.1 Order schema and its imports.  These are the official 04 Nov
# 2013 OASIS files; the paths mirror the relative schemaLocation imports.
UBL = "https://docs.oasis-open.org/ubl/os-UBL-2.1/xsd/"
XSD_FILES = {
    "maindoc/UBL-Order-2.1.xsd": "81c56acdcd9cb34411f14d1de50ebc38d87ee2dd11db426c327da6683175cb38",
    "common/UBL-CommonExtensionComponents-2.1.xsd": "ad7a4e490978adfbcfc5ec0bb20941cf11ac960ccf0c4de8791a7c731a8dbe87",
    "common/UBL-ExtensionContentDataType-2.1.xsd": "fcee77a11870208e6377ea6311b9f2a050bca24bdad8606ea02d71e9f9e72f8d",
    "common/UBL-CommonSignatureComponents-2.1.xsd": "3db472305f029bba5c1ae157bfd0178f715c3f9b94bd8e6c557dbce5e88da874",
    "common/UBL-SignatureAggregateComponents-2.1.xsd": "17bb6b62d709b4fd81449a37655af36aa6a1276ad4fdb1b2e249a5ed4b7c2172",
    "common/UBL-XAdESv141-2.1.xsd": "1fa4625e9cefcb7a9abb5ac1b64315547450031eece8a55bd584e4ba4b79dbc1",
    "common/UBL-XAdESv132-2.1.xsd": "a4f726bcf8cc3f7d9ffa4dab99e005535a8e8b60dced1e5d94578d2e05afa96e",
    "common/UBL-xmldsig-core-schema-2.1.xsd": "101909c9f06456d61ddcc4fb982f1d40dc357b439f393b1a2eb46e42acd60809",
    "common/UBL-CommonBasicComponents-2.1.xsd": "bd4ad043ee1d9da1c7f8018dabf739cfafdfb59143d0d16b9ef769e6b7c408a7",
    "common/UBL-UnqualifiedDataTypes-2.1.xsd": "09052d406b4293e2a5f9c2bfee6df10ad4d8d5f0b36e24a6349d7f7936d89eb6",
    "common/CCTS_CCT_SchemaModule-2.1.xsd": "dd546e4809df86b6445589f69f0d6c9df162840ae386574ddfc1da7638103e15",
    "common/UBL-QualifiedDataTypes-2.1.xsd": "7dcb156e610239c97ae70940cf4653b88e48c3595bf5f56a2204a32e2893e6cf",
    "common/UBL-SignatureBasicComponents-2.1.xsd": "cef924d7ba3d1d8ade14469325cde1364f8c174e46f0198ec02da8e9e748a489",
    "common/UBL-CommonAggregateComponents-2.1.xsd": "939172ad8dd057cd403e7f763f6532184dd5ed4b9de24c42ebb35db4792ba613",
}

# Generated Schematron published from https://github.com/OpenPEPPOL/poacc-upgrade-3
# (T01, May 2026 release).  The archive contains the generated basic rules as well as the
# business rules, so this is the complete official rule set rather than a subset.
SCH_URL = "https://docs.peppol.eu/poacc/upgrade-3/files/PEPPOLBIS-Upgrade-Schematron.zip"
SCH_SHA256 = "10d8f2af8564acada635493335481d1e4a5aae42cfbf8b401569f47441bc1d48"
SCH_T01_SHA256 = "64675e570cceba9fe076e1dc10330cfcb377b5ed93bd27d1083354cd39a780af"

# ISO Schematron's XSLT 2 compiler and SVRL output implementation.  Saxonche
# executes the resulting stylesheet with XPath 2.0/3.0 semantics.
SCH_XSL = {
    "iso_dsdl_include.xsl": ("https://raw.githubusercontent.com/Schematron/schematron/master/trunk/schematron/code/iso_dsdl_include.xsl", "43ff20a1afd89d8a744d1c0b8df94ac5559ffa6a820d1ffbf508d6431ee4fdd9"),
    "iso_abstract_expand.xsl": ("https://raw.githubusercontent.com/Schematron/schematron/master/trunk/schematron/code/iso_abstract_expand.xsl", "c5267f124abf23eeb6669884e40a98607c055bfaa1f39e73b7d578feceeb6e46"),
    "iso_svrl_for_xslt2.xsl": ("https://raw.githubusercontent.com/Schematron/schematron/master/trunk/schematron/code/iso_svrl_for_xslt2.xsl", "0588d617924a0686255f6d182633d434c7986d561be8fcc3b363907d3f671b26"),
    "iso_schematron_skeleton_for_saxon.xsl": ("https://raw.githubusercontent.com/Schematron/schematron/master/trunk/schematron/code/iso_schematron_skeleton_for_saxon.xsl", "95f3195d9f437ea8ff5f75d1a27f4e68ae20b236fe0d4a217bb4209f498a10a3"),
}


def _get(url: str, path: Path, sha256: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
        return path
    try:
        data = urllib.request.urlopen(url, timeout=45).read()
    except Exception as exc:  # network failures must fail loudly, never skip validation
        raise AssertionError(f"could not fetch official Peppol artifact {url}: {exc}") from exc
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise AssertionError(f"SHA-256 changed for {url}: expected {sha256}, got {got}")
    path.write_bytes(data)
    return path


def _prepare(cache: Path) -> tuple[Path, Path]:
    for rel, sha in XSD_FILES.items():
        _get(UBL + rel, cache / "ubl" / rel, sha)
    zip_path = _get(SCH_URL, cache / "openpeppol" / "PEPPOLBIS-Upgrade-Schematron.zip", SCH_SHA256)
    sch_dir = cache / "openpeppol" / "schematron"
    t01 = sch_dir / "PEPPOLBIS-T01.sch"
    if not t01.exists():
        with zipfile.ZipFile(io.BytesIO(zip_path.read_bytes())) as zf:
            try:
                raw = zf.read("schematron/PEPPOLBIS-T01.sch")
            except KeyError as exc:
                raise AssertionError("the official Schematron archive has no T01 rule file") from exc
        sch_dir.mkdir(parents=True, exist_ok=True)
        t01.write_bytes(raw)
    if hashlib.sha256(t01.read_bytes()).hexdigest() != SCH_T01_SHA256:
        raise AssertionError("cached T01 Schematron does not match the pinned OpenPeppol artifact")
    for name, (url, sha) in SCH_XSL.items():
        _get(url, cache / "compiler" / name, sha)
    return cache / "ubl" / "maindoc" / "UBL-Order-2.1.xsd", t01


class Validators:
    def __init__(self, cache: Path):
        xsd, sch = _prepare(cache)
        self.schema = etree.XMLSchema(etree.parse(str(xsd)))
        compiler = cache / "compiler"
        included, expanded, stylesheet = (compiler / n for n in ("T01.included.sch", "T01.expanded.sch", "T01.validator.xsl"))
        with PySaxonProcessor(license=False) as proc:
            xp = proc.new_xslt30_processor()
            xp.compile_stylesheet(stylesheet_file=str(compiler / "iso_dsdl_include.xsl")).transform_to_file(source_file=str(sch), output_file=str(included))
            xp.compile_stylesheet(stylesheet_file=str(compiler / "iso_abstract_expand.xsl")).transform_to_file(source_file=str(included), output_file=str(expanded))
            xp.compile_stylesheet(stylesheet_file=str(compiler / "iso_svrl_for_xslt2.xsl")).transform_to_file(source_file=str(expanded), output_file=str(stylesheet))
        self.stylesheet = stylesheet

    def check(self, label: str, xml: str) -> None:
        doc = etree.fromstring(xml.encode("utf-8"))
        self.schema.assertValid(doc)
        with PySaxonProcessor(license=False) as proc:
            xp = proc.new_xslt30_processor()
            result = xp.compile_stylesheet(stylesheet_file=str(self.stylesheet)).transform_to_string(xdm_node=proc.parse_xml(xml_text=xml))
        svrl = etree.fromstring(result.encode("utf-8"))
        failures = svrl.xpath("//*[local-name()='failed-assert']")
        errors = [f for f in failures if f.get("flag") in ("fatal", "error")]
        warnings = [f for f in failures if f.get("flag") == "warning"]
        for warning in warnings:
            text = " ".join(warning.itertext()).strip()
            print(f"WARNING {label} {warning.get('id')}: {text}")
        assert not errors, f"{label}: Peppol T01 errors: " + "; ".join(" ".join(e.itertext()).strip() for e in errors)
        print(f"ok   {label}: UBL 2.1 schema and OpenPeppol T01 Schematron (0 errors, {len(warnings)} warnings)")


g = Gateway()
# This suite owns its scratch database.  Wiping it makes a rerun with the same
# AIMO_DATA deterministic and cannot touch the live server's data folder.
g.wipe()
D = "2026-11-16"
n = 0


def principal(slug: str):
    g.create_account(slug, f"Peppol {slug} kitchen", kind="sandbox")
    token = g.issue_mandate(slug, agent=f"agent:{slug}/peppol", name="Peppol test agent")
    return g.identify(token), token


def place(p, token, lines, trace, day=D):
    c = g.create_checkout({"line_items": lines, "wholesale": {"delivery_date": day}}, p, trace)
    if c["status"] == "requires_escalation":
        g.decide(c["id"], c["continue_url"].split("k=", 1)[1], True, "Peppol test approver", "validated test order")
    assert c["status"] in ("ready_for_complete", "requires_escalation"), c["messages"]
    c = g.get_checkout(c["id"], p)
    done = g.complete_checkout(c["id"], {"ap2": {"checkout_mandate": g.agent_proof(c, token)}}, p)
    assert done["status"] == "completed", done
    return g.get_order(done["order"]["id"])


def check_order(v: Validators, label: str, order: dict):
    global n
    n += 1
    v.check(label, peppol.order_xml(order))


# 1. A plain order.
p, t = principal("peppol-plain")
plain = place(p, t, [{"item": {"id": "464201"}, "quantity": 10}], "peppol-plain")

# 2. A split order containing a listed substitute.
p, t = principal("peppol-substitute")
plan = g.plan({"query": "lactose-free milk", "need": {"quantity": 1000, "unit": "L"}, "preferred_product": "463380", "delivery_date": D}, p, "peppol-substitute")
split = next(o for o in plan["options"] if o["label"] == "split_with_substitute")
substitute = place(p, t, split["line_items"], "peppol-substitute")
assert any(l["wholesale"].get("substitutes_for") for l in substitute["line_items"])

# 3. The checked wedding menu becomes one order with many lines.
p, t = principal("peppol-menu")
menu_plan = menu.build_plan(g, menu.normalize(menu.WEDDING, "card"), p)
assert menu_plan["status"] == "ready" and len(menu_plan["checkout"]["line_items"]) >= 30
menu_order = place(p, t, menu_plan["checkout"]["line_items"], "peppol-menu", menu_plan["delivery_date"])
assert len(menu_order["line_items"]) >= 30

# 4. An order amended after a short pick.  The original confirmation is still
# immutable; its Peppol export remains the document that was ordered.
p, t = principal("peppol-amended")
original = place(p, t, [{"item": {"id": "464201"}, "quantity": 400}], "peppol-amended")
issue = g.after.raise_issue(original, "short_pick")
sub_opts = [w for w in issue["notice"]["ways_out"] if w["option"].startswith("substitute_")]
assert sub_opts, "short pick did not produce a substitute way out"
amend = g.after.resolve(issue["id"], sub_opts[0]["option"], "The kitchen needs the missing goods.", p, "peppol-amend")
if amend["status"] == "requires_escalation":
    g.decide(amend["id"], amend["continue_url"].split("k=", 1)[1], True, "Peppol test approver", "validated amendment")
amend = g.get_checkout(amend["id"], p)
done = g.complete_checkout(amend["id"], {"ap2": {"checkout_mandate": g.agent_proof(amend, t)}}, p)
assert done["status"] == "completed"
amended_state = g.after.state(original)
assert amended_state["status"] == "amended" and amended_state["amendments"]


cache_root = Path(os.environ.get("AIMO_DATA", "/tmp/aimo-peppol")) / "peppol-cache"
validators = Validators(cache_root)
check_order(validators, "plain", plain)
check_order(validators, "substitute", substitute)
check_order(validators, "menu-many-lines", menu_order)
check_order(validators, "amended-after-short-pick", original)

# Every sales unit in the product data must come out as a unit code Peppol accepts, not only the units these four orders happen to use.
sch = (cache_root / "openpeppol" / "schematron" / "PEPPOLBIS-T01.sch").read_text()
at = sch.find("Recommendation 20, including Recommendation 21")
allowed = set(re.findall(r"tokenize\('([^']{2000,})'", sch[max(0, at - 200000):at])[-1].split())
units = {g._qunit(d)["unit"] for d in g.cat.docs}
bad = sorted(u for u in units if peppol.PEPPOL_UNITS.get(u, u) not in allowed)
assert not bad, f"sales units with no unit code on Peppol's list: {bad}"
n += 1
print(f"ok   all {len(units)} sales units in the product data map to a unit code on Peppol's list")

print(f"ALL {n} CHECKS PASS")
