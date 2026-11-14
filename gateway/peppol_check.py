"""Validate one Peppol order document the way an access point would before sending it.

Two checks, both against the published artefacts, each pinned here by SHA-256: the OASIS UBL 2.1 Order schema and
OpenPeppol's own T01 (Order) Schematron rules. The files are fetched once into the data folder and compared with the
pins; a changed upstream file is refused, never used. tests/peppol.py runs the same two checks on four kinds of order.

What this is not: the document is not sent anywhere. This prototype has no Peppol access point and no participant id
registered in an SMP, and the GLNs in the document are test numbers.
"""
from __future__ import annotations

import hashlib
import io
import threading
import urllib.request
import zipfile
from pathlib import Path

from .config import DATA

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
SCH_URL = "https://docs.peppol.eu/poacc/upgrade-3/files/PEPPOLBIS-Upgrade-Schematron.zip"
SCH_SHA256 = "10d8f2af8564acada635493335481d1e4a5aae42cfbf8b401569f47441bc1d48"
SCH_T01_SHA256 = "64675e570cceba9fe076e1dc10330cfcb377b5ed93bd27d1083354cd39a780af"
SCH_XSL = {
    "iso_dsdl_include.xsl": ("https://raw.githubusercontent.com/Schematron/schematron/master/trunk/schematron/code/iso_dsdl_include.xsl", "43ff20a1afd89d8a744d1c0b8df94ac5559ffa6a820d1ffbf508d6431ee4fdd9"),
    "iso_abstract_expand.xsl": ("https://raw.githubusercontent.com/Schematron/schematron/master/trunk/schematron/code/iso_abstract_expand.xsl", "c5267f124abf23eeb6669884e40a98607c055bfaa1f39e73b7d578feceeb6e46"),
    "iso_svrl_for_xslt2.xsl": ("https://raw.githubusercontent.com/Schematron/schematron/master/trunk/schematron/code/iso_svrl_for_xslt2.xsl", "0588d617924a0686255f6d182633d434c7986d561be8fcc3b363907d3f671b26"),
    "iso_schematron_skeleton_for_saxon.xsl": ("https://raw.githubusercontent.com/Schematron/schematron/master/trunk/schematron/code/iso_schematron_skeleton_for_saxon.xsl", "95f3195d9f437ea8ff5f75d1a27f4e68ae20b236fe0d4a217bb4209f498a10a3"),
}
RULES = {"schema": "OASIS UBL 2.1 Order schema", "schematron": "OpenPeppol BIS Ordering 3 rules for the Order (T01), May 2026 release", "pinned": "every file by SHA-256 in gateway/peppol_check.py"}

_lock = threading.Lock()
_ready: dict = {}


def _get(url: str, path: Path, sha256: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == sha256:
        return path
    data = urllib.request.urlopen(url, timeout=45).read()
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError(f"{url} is not the file pinned here")
    path.write_bytes(data)
    return path


def _prepare(cache: Path):
    """Fetch the pinned files once and compile the rules into one stylesheet."""
    from lxml import etree
    from saxonche import PySaxonProcessor
    for rel, sha in XSD_FILES.items():
        _get(UBL + rel, cache / "ubl" / rel, sha)
    zp = _get(SCH_URL, cache / "openpeppol" / "PEPPOLBIS-Upgrade-Schematron.zip", SCH_SHA256)
    t01 = cache / "openpeppol" / "schematron" / "PEPPOLBIS-T01.sch"
    if not t01.exists():
        t01.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(zp.read_bytes())) as zf:
            t01.write_bytes(zf.read("schematron/PEPPOLBIS-T01.sch"))
    if hashlib.sha256(t01.read_bytes()).hexdigest() != SCH_T01_SHA256:
        raise ValueError("the cached T01 rule file is not the one pinned here")
    comp = cache / "compiler"
    for name, (url, sha) in SCH_XSL.items():
        _get(url, comp / name, sha)
    sheet = comp / "T01.validator.xsl"
    if not sheet.exists():
        inc, exp = comp / "T01.included.sch", comp / "T01.expanded.sch"
        with PySaxonProcessor(license=False) as proc:
            xp = proc.new_xslt30_processor()
            xp.compile_stylesheet(stylesheet_file=str(comp / "iso_dsdl_include.xsl")).transform_to_file(source_file=str(t01), output_file=str(inc))
            xp.compile_stylesheet(stylesheet_file=str(comp / "iso_abstract_expand.xsl")).transform_to_file(source_file=str(inc), output_file=str(exp))
            xp.compile_stylesheet(stylesheet_file=str(comp / "iso_svrl_for_xslt2.xsl")).transform_to_file(source_file=str(exp), output_file=str(sheet))
    return etree.XMLSchema(etree.parse(str(cache / "ubl" / "maindoc" / "UBL-Order-2.1.xsd"))), sheet


def check(xml: str) -> dict:
    """{ok, schema, errors: [{id, text}], warnings: [{id, text}], fired, rules} for one Order document, or {ok: None, why} when the rule files cannot be had."""
    from lxml import etree
    from saxonche import PySaxonProcessor
    with _lock:  # Saxon is not shared between threads here; one check at a time is plenty
        try:
            if not _ready:
                _ready["schema"], _ready["sheet"] = _prepare(DATA / "peppol-cache")
        except Exception as e:
            return {"ok": None, "why": f"The published rule files could not be fetched or did not match their pins ({type(e).__name__}).", "rules": RULES}
        doc = etree.fromstring(xml.encode("utf-8"))
        schema_ok = _ready["schema"].validate(doc)
        schema_errors = [{"id": "UBL-2.1", "text": str(e.message)} for e in _ready["schema"].error_log][:10]
        with PySaxonProcessor(license=False) as proc:
            xp = proc.new_xslt30_processor()
            svrl = etree.fromstring(xp.compile_stylesheet(stylesheet_file=str(_ready["sheet"])).transform_to_string(xdm_node=proc.parse_xml(xml_text=xml)).encode("utf-8"))
    failed = svrl.xpath("//*[local-name()='failed-assert']")
    row = lambda f: {"id": f.get("id"), "text": " ".join(" ".join(f.itertext()).split())}
    errors = schema_errors + [row(f) for f in failed if f.get("flag") in ("fatal", "error")]
    warnings = [row(f) for f in failed if f.get("flag") == "warning"]
    return {"ok": schema_ok and not errors, "schema": bool(schema_ok), "errors": errors, "warnings": warnings,
            "fired": len(svrl.xpath("//*[local-name()='fired-rule']")), "rules": RULES}
